"""Independent completed-batch audit against the frozen original exporter."""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime,timezone
import json
import multiprocessing as mp
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace

import numpy as np

from upstream import run_realtime_wrf_platform_cache as reference
from run_native import validate_raw,atomic_json
from exporter import file_digest,NATIVE_MODEL


def parameters(path):
    import re
    result={}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        match=re.match(r'\s*([a-zA-Z_0-9]+)\s*=\s*(.*?)\s*(?:!.*)?$',line)
        if match:result[match[1].lower()]=re.sub(r'\s+','',match[2]).lower().rstrip(',')
    return result


def reference_uv(dataset,heights):
    """Independently repeat frozen production extraction for EVERY raw frame."""
    xlat=reference.read_time_slice(dataset.variables['XLAT'],0)
    xlon=reference.read_time_slice(dataset.variables['XLONG'],0)
    lons,lats,mapper=reference.regular_grid_mapper(xlat,xlon,None)
    shape=(len(lats),len(lons))
    cos=reference.read_time_slice(dataset.variables['COSALPHA'],0) if 'COSALPHA' in dataset.variables else np.ones_like(xlat)
    sin=reference.read_time_slice(dataset.variables['SINALPHA'],0) if 'SINALPHA' in dataset.variables else np.zeros_like(xlat)
    u=reference.as_array(dataset.variables['U'][0]);v=reference.as_array(dataset.variables['V'][0])
    u=.5*(u[:,:,:-1]+u[:,:,1:]);v=.5*(v[:,:-1,:]+v[:,1:,:])
    u,v=reference.rotate_to_earth(u,v,cos[np.newaxis,:,:],sin[np.newaxis,:,:])
    ph=reference.as_array(dataset.variables['PH'][0]);phb=reference.as_array(dataset.variables['PHB'][0])
    terrain=reference.read_time_slice(dataset.variables['HGT'],0)
    interfaces=(ph+phb)/reference.GRAVITY
    agl=.5*(interfaces[:-1]+interfaces[1:])-terrain[np.newaxis,:,:]
    u10=v10=None
    if 'U10' in dataset.variables and 'V10' in dataset.variables:
        u10,v10=reference.rotate_to_earth(reference.read_time_slice(dataset.variables['U10'],0),
            reference.read_time_slice(dataset.variables['V10'],0),cos,sin)
    u,v=reference.interpolate_levels(agl,u,v,u10,v10,np.asarray(heights,dtype=np.float32))
    return reference.apply_mapper(u,shape,mapper),reference.apply_mapper(v,shape,mapper)


def validate_product(task):
    root,row,raw_path=task
    minute=row['forecast_minutes'];p=root/row['path']
    assert p.stat().st_size==row['size_bytes'] and file_digest(p)==row['sha256']
    with np.load(p,allow_pickle=False) as data:
        assert int(data['forecast_minutes'].item())==minute
        assert float(data['forecast_hour'].item())==minute/60
        assert int(data['output_interval_seconds'].item())==600
        assert str(data['wind_time_model'].item())==NATIVE_MODEL
        assert str(data['cycle_utc'].item())==row['cycle']
        assert str(data['valid_time_utc'].item())==row['valid_time']
        assert str(data['source_wrf_sha256'].item())==file_digest(raw_path)
        u,v,levels=data['u'],data['v'],data['levels_m']
        assert u.shape==v.shape==(len(levels),len(data['lats']),len(data['lons']))
        assert np.isfinite(u).any() and np.isfinite(v).any()
        with reference.open_wrf_dataset(raw_path) as dataset:
            assert float(dataset.DT)==15
            expected_u,expected_v=reference_uv(dataset,levels)
            assert np.array_equal(u,expected_u,equal_nan=True),'Raw U extraction mismatch: %d'%minute
            assert np.array_equal(v,expected_v,equal_nan=True),'Raw V extraction mismatch: %d'%minute
        return dict(minute=minute,levels=levels.tolist(),sha256=row['sha256'])


def validate(base,cycle,hours=48,workers=8):
    began=monotonic();label=cycle.strftime('%Y%m%d%H')
    root=base/'data/wrf_platform_cache_10min'
    index=json.loads((root/'index.json').read_text(encoding='utf-8'))
    rows=[r for r in index['files'] if r['cycle']==reference.format_utc(cycle)]
    assert index['wind_time_model']==NATIVE_MODEL and index['output_interval_seconds']==600
    assert {r['forecast_minutes'] for r in rows}==set(range(0,hours*60+1,10))
    assert len(rows)==hours*6+1 and len({r['path'] for r in rows})==len(rows)
    raw=validate_raw(base/'wrf_output_10min'/label,cycle,hours)
    success=(base/'WRF/test/em_real/rsl.error.0000').read_text(errors='replace')
    assert 'SUCCESS COMPLETE WRF' in success,'Missing WRF successful-completion marker'
    parent_times=[]
    for path in sorted((base/'WRF/test/em_real').glob('wrfout_d01_*')):
        with reference.open_wrf_dataset(path) as dataset:
            assert getattr(dataset,'SIMULATION_START_DATE',None)==cycle.strftime('%Y-%m-%d_%H:%M:%S')
            assert float(dataset.DT)==45
            parent_times.extend(reference.read_times(dataset))
    assert [(stamp-cycle).total_seconds() for stamp in parent_times]==list(range(0,hours*3600+1,3600))
    checks=[]
    tasks=[(root,row,raw[row['forecast_minutes']]) for row in rows]
    with ProcessPoolExecutor(max_workers=workers,mp_context=mp.get_context('spawn')) as pool:
        for check in pool.map(validate_product,tasks):
            if checks:assert check['levels']==checks[0]['levels']
            checks.append(check)
            if len(checks)%24==0 or len(checks)==len(rows):
                print('[AUDIT] verified %d/%d raw/NPZ U/V pairs'%(len(checks),len(rows)),flush=True)
    samples=sorted(set(m for m in [0,10,50,60,70,610,1230,2250,hours*60] if m<=hours*60))
    compared=[]
    with TemporaryDirectory(prefix='native-reference-',dir=str(base/'operations/native_wrf_10min')) as tmp:
        for minute in samples:
            args=SimpleNamespace(heights=reference.DEFAULT_HEIGHTS_M,output_spacing_deg=None,domain=2,
                cache_dir=Path(tmp)/str(minute),export_start_fhour=0,forecast_hours=hours)
            record=reference.export_wrf_file(raw[minute],cycle,args)[0]
            actual=root/next(row['path'] for row in rows if row['forecast_minutes']==minute)
            variables=[]
            with np.load(args.cache_dir/record.relative_path,allow_pickle=False) as expected,np.load(actual,allow_pickle=False) as result:
                for variable in expected.files:
                    if variable in ('forecast_hour',):continue
                    a,b=expected[variable],result[variable]
                    equal=np.array_equal(a,b,equal_nan=True) if a.dtype.kind in 'fc' else np.array_equal(a,b)
                    assert equal,'Numerical mismatch at minute %d: %s'%(minute,variable)
                    variables.append(variable)
            compared.append(dict(minute=minute,variables=variables))
    before=parameters(Path(__file__).parent/'upstream/WRF/test/em_real/namelist.input')
    after=parameters(base/'WRF/test/em_real/namelist.input')
    ignored={'history_interval','run_days','run_hours','run_minutes','run_seconds','time_step'}
    ignored.update(k for k in before if k.startswith('start_') or k.startswith('end_'))
    different={k:(v,after.get(k)) for k,v in before.items() if k not in ignored and after.get(k)!=v}
    assert not different,'Unexpected namelist changes: '+str(different)
    assert after['history_interval']=='60,10' and after['frames_per_outfile']=='1,1' and after['interval_seconds']=='3600'
    assert before['time_step']=='48' and after['time_step']=='45'
    legacy=json.loads((base/'data/wrf_platform_cache/index.json').read_text(encoding='utf-8'))
    hourly=[r for r in legacy['files'] if r['cycle']==reference.format_utc(cycle)]
    assert {r['forecast_hour'] for r in hourly}==set(range(1,hours+1))
    return dict(verified_at=datetime.now(timezone.utc).isoformat(),cycle=reference.format_utc(cycle),frames=len(rows),
        raw_frames=len(raw),all_raw_hashes_verified=True,all_npz_hashes_verified=True,
        wrf_success_marker=True,parent_output_frames=len(parent_times),parent_output_interval_seconds=3600,
        all_raw_npz_uv_comparisons=len(checks),all_raw_npz_uv_exactly_equal=True,
        validation_workers=workers,
        sampled_original_export_comparisons=compared,levels=checks[0]['levels'],
        cache_variables=index.get('cache_variables',[]),
        wind_time_model=NATIVE_MODEL,output_interval_seconds=600,physics_and_grid_unchanged=True,
        parent_time_step_seconds=45,child_time_step_seconds=15,
        hourly_compatibility_frames=len(hourly),validation_seconds=monotonic()-began,
        native_cache_bytes=sum(r['size_bytes'] for r in rows),raw_bytes=sum(p.stat().st_size for p in raw.values()))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--cycle',required=True)
    parser.add_argument('--base-dir',type=Path,default=Path('/root/pyWRF-automation'))
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    cycle=datetime.strptime(args.cycle,'%Y%m%d%H').replace(tzinfo=timezone.utc)
    result=validate(args.base_dir,cycle)
    atomic_json(args.output,result)
    print(json.dumps(result))
