#!/usr/bin/env python3
"""One production producer: native d02 batches and an hourly legacy view."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import numpy as np

from upstream import run_realtime_wrf_platform_cache as legacy
from exporter import export_wrf_file,file_digest,NATIVE_MODEL


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        with temporary.open('w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        os.replace(str(temporary),str(path))
    finally:
        if temporary.exists():temporary.unlink()


def expected_minutes(hours):return set(range(0,hours*60+1,10))


def choose_available_cycle(args):
    """A published f001 alone does not establish a usable 48-hour forcing set."""
    anchor=args.now or datetime.now(timezone.utc)-timedelta(hours=args.delay_hours)
    latest=legacy.floor_to_gfs_cycle(anchor)
    for offset in range(args.cycle_fallback_count):
        cycle=latest-timedelta(hours=6*offset)
        tail=legacy.gdex_style_path(args.gfs_dir,cycle,args.forecast_hours)
        if not legacy.is_valid_file(tail,args.min_bytes):
            if not legacy.probe_cycle(cycle,args):continue
            print('[NATIVE] checking full forecast tail '+legacy.format_utc(cycle),flush=True)
            if not legacy.download(legacy.build_gfs_url(cycle,args.forecast_hours,args.gfs_vars,args.gfs_levels),
                tail,1,min(args.timeout,90),args.min_bytes):
                print('[NATIVE] forecast tail not published; try previous cycle',flush=True);continue
        try:legacy.download_wrf_forcing(cycle,args)
        except SystemExit as exc:
            print('[NATIVE] incomplete forcing, try previous cycle: '+str(exc),flush=True);continue
        print('[NATIVE] complete forcing selected '+legacy.format_utc(cycle),flush=True)
        return cycle
    raise RuntimeError('No complete 48-hour GFS forcing cycle is available')


def validate_raw(directory,cycle,hours):
    rows={}
    for path in sorted(directory.rglob('wrfout_d02_*')):
        with legacy.open_wrf_dataset(path) as dataset:
            if getattr(dataset,'SIMULATION_START_DATE',None)!=cycle.strftime('%Y-%m-%d_%H:%M:%S'):
                raise ValueError('WRF raw batch contains a different simulation lineage')
            if not {'U','V','PH','PHB','HGT','Times'}.issubset(dataset.variables):
                raise ValueError('WRF raw output missing mandatory variables')
            times=legacy.read_times(dataset)
            if len(times)!=1:raise ValueError('Expected one time per native output file')
            minute=(times[0]-cycle).total_seconds()/60
            if not minute.is_integer() or minute%10 or minute in rows:raise ValueError('Duplicate or unaligned WRF time')
            rows[int(minute)]=path
    if set(rows)!=expected_minutes(hours):raise ValueError('Native raw batch is incomplete: %d/%d'%(len(rows),hours*6+1))
    return rows


def index_payload(records,args,legacy_view=False):
    root=args.legacy_cache_dir if legacy_view else args.cache_dir
    old=[]
    if (root/'index.json').is_file():old=json.loads((root/'index.json').read_text(encoding='utf-8'))['files']
    merged={(row['cycle'],row['valid_time']):row for row in old if (root/row['path']).is_file()}
    merged.update({(row['cycle'],row['valid_time']):row for row in records})
    rows=sorted(merged.values(),key=lambda row:(row['cycle'],row['valid_time']))
    by_valid={}
    for row in rows:by_valid[row['valid_time']]={k:row[k] for k in ('cycle','cycle_bj','forecast_hour','valid_time','valid_time_bj','forecast_minutes') if k in row}
    valid=[dict(row,label=row['valid_time_bj']) for _,row in sorted(by_valid.items())]
    return dict(source='WRF d02 '+('hourly compatibility view' if legacy_view else 'native ten-minute cache'),
        generated_at_utc=datetime.now(timezone.utc).isoformat(),schema_version=2,
        wind_time_model='HOURLY_UV_TEN_MINUTE_HOLD_V1' if legacy_view else NATIVE_MODEL,
        output_interval_seconds=3600 if legacy_view else 600,forecast_duration_minutes=args.forecast_hours*60,
        levels=['%dm AGL'%h for h in args.heights],cache_variables=args.cache_variables,
        cycles=sorted({r['cycle'] for r in rows}),forecast_hours=sorted({r['forecast_hour'] for r in rows}),
        forecast_hours_by_cycle={c:sorted({r['forecast_hour'] for r in rows if r['cycle']==c}) for c in sorted({r['cycle'] for r in rows})},
        valid_times=valid,files=rows)


def publish(cycle,raw,args):
    label=cycle.strftime('%Y%m%d%H')
    stage=args.cache_dir/('.staging-'+uuid4().hex);stage.mkdir(parents=True)
    from copy import copy
    export_args=copy(args);export_args.cache_dir=stage
    records=[]
    try:
        for minute,path in sorted(raw.items()):
            result=export_wrf_file(path,cycle,export_args)
            if len(result)!=1:raise ValueError('Exactly one NPZ per raw time is required')
            record=result[0];p=stage/record.relative_path
            with np.load(p,allow_pickle=False) as data:
                if int(data['forecast_minutes'].item())!=minute or str(data['wind_time_model'].item())!=NATIVE_MODEL:
                    raise ValueError('Minute export identity mismatch')
                args.cache_variables=sorted(set(['u','v']+json.loads(str(data['cache_variables'].item()))))
            records.append(dict(cycle=legacy.format_utc(cycle),cycle_bj=legacy.format_bj(cycle),
                forecast_hour=minute/60,forecast_minutes=minute,valid_time=legacy.format_utc(record.valid_time),
                valid_time_bj=legacy.format_bj(record.valid_time),path=record.relative_path,bbox=record.bbox,
                size_bytes=p.stat().st_size,sha256=file_digest(p),wind_time_model=NATIVE_MODEL,output_interval_seconds=600))
        if {r['forecast_minutes'] for r in records}!=expected_minutes(args.forecast_hours):raise ValueError('Native NPZ batch incomplete')
        destination=args.cache_dir/label
        if destination.exists():
            # Recovery after a successful directory commit but an interrupted
            # legacy/index publication must not overwrite immutable files.
            for row in records:
                p=args.cache_dir/row['path'];new=stage/row['path']
                with np.load(p,allow_pickle=False) as a,np.load(new,allow_pickle=False) as b:
                    if set(a.files)!=set(b.files) or any(not np.array_equal(a[k],b[k],equal_nan=True)
                        if a[k].dtype.kind in 'fc' else not np.array_equal(a[k],b[k]) for k in a.files):
                        raise ValueError('Refusing to overwrite a different immutable native batch')
                row.update(size_bytes=p.stat().st_size,sha256=file_digest(p))
        else:os.replace(str(stage/label),str(destination))
        # The old platform sees only whole-hour frames and its original names.
        hourly=[]
        for row in records:
            minute=row['forecast_minutes']
            if minute==0 or minute%60:continue
            hour=minute//60
            relative=Path(label)/('wrf_d02_%s_f%03d.npz'%(label,hour))
            target=args.legacy_cache_dir/relative;target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_name(target.name+'.'+uuid4().hex+'.tmp')
            shutil.copy2(str(args.cache_dir/row['path']),str(temporary));os.replace(str(temporary),str(target))
            hourly.append(dict(row,path=relative.as_posix(),forecast_hour=hour))
        atomic_json(args.legacy_cache_dir/'index.json',index_payload(hourly,args,True))
        atomic_json(args.cache_dir/'index.json',index_payload(records,args))
        print('[NATIVE] published %s: %d native / %d legacy frames'%(label,len(records),len(hourly)),flush=True)
        return records
    finally:
        if stage.exists():shutil.rmtree(str(stage))


def complete(cycle,args):
    p=args.cache_dir/'index.json'
    if not p.is_file():return False
    index=json.loads(p.read_text(encoding='utf-8'));rows=[r for r in index['files'] if r['cycle']==legacy.format_utc(cycle)]
    return (index.get('wind_time_model')==NATIVE_MODEL and {r.get('forecast_minutes') for r in rows}==expected_minutes(args.forecast_hours)
            and all((args.cache_dir/r['path']).is_file() and (args.cache_dir/r['path']).stat().st_size==r['size_bytes'] for r in rows))


def cleanup(args):
    # Preserve the original three-day cleanup of GFS and the hourly view.
    from copy import copy
    old=copy(args);old.wrfout_dir=args.base_dir/'wrf_output';old.cache_dir=args.legacy_cache_dir
    legacy.cleanup_old_products(old)
    cutoff=(datetime.now(timezone.utc)-timedelta(days=args.keep_days)).strftime('%Y%m%d')
    if args.keep_days<=0:return
    for root in (args.wrfout_dir,args.cache_dir):
        root=root.resolve()
        for path in root.iterdir() if root.exists() else []:
            if path.is_dir() and not path.is_symlink() and re.fullmatch(r'\d{10}',path.name) and path.name[:8]<cutoff:
                if path.resolve().parent!=root:raise ValueError('Cleanup escaped native root')
                shutil.rmtree(str(path))
    if (args.cache_dir/'index.json').is_file():
        index=json.loads((args.cache_dir/'index.json').read_text(encoding='utf-8'))
        index['files']=[r for r in index['files'] if (args.cache_dir/r['path']).is_file()]
        atomic_json(args.cache_dir/'index.json',index)


@contextmanager
def production_lock(base):
    import fcntl
    with (base/'.native_wrf_production.lock').open('a') as stream:
        fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def once(args):
    cleanup(args)
    cycle=choose_available_cycle(args)
    if complete(cycle,args):
        print('[NATIVE] complete batch already exists: '+legacy.format_utc(cycle),flush=True);return
    label=cycle.strftime('%Y%m%d%H');destination=args.wrfout_dir/label
    if destination.exists():
        raw=validate_raw(destination,cycle,args.forecast_hours)
    else:
        stage=args.wrfout_dir/('.staging-'+label+'-'+uuid4().hex);stage.mkdir(parents=True)
        legacy.prepare_namelist(args)
        env={**os.environ,'WRF_START_DATE':cycle.strftime('%Y-%m-%d_%H:%M:%S'),
            'GFS_CYCLE_TIME':cycle.strftime('%Y-%m-%d_%H:%M:%S'),'WRF_FORECAST_HOURS':str(args.forecast_hours),
            'WRF_GFS_INTERVAL_HOURS':str(args.gfs_interval_hours),'WRF_NUM_PROC':str(args.num_proc),
            'REUSE_GEOGRID':'1' if args.reuse_geogrid else '0'}
        subprocess.run([sys.executable,str(Path(__file__).with_name('native_runner.py')),'--output-dir',str(stage)],
            cwd=str(args.base_dir),env=env,check=True)
        raw=validate_raw(stage,cycle,args.forecast_hours)
        for path in raw.values():shutil.move(str(path),str(stage/path.name))
        for folder in [p for p in stage.iterdir() if p.is_dir()]:folder.rmdir()
        os.replace(str(stage),str(destination))
        raw=validate_raw(destination,cycle,args.forecast_hours)
    publish(cycle,raw,args)


def parse_args():
    # Keep the proven producer's forcing, physics and retention options.
    args=legacy.parse_args()
    if not any(v=='--forecast-hours' or v.startswith('--forecast-hours=') for v in sys.argv[1:]):args.forecast_hours=48
    if not any(v=='--num-proc' or v.startswith('--num-proc=') for v in sys.argv[1:]):args.num_proc=16
    args.wrfout_dir=args.base_dir/'wrf_output_10min'
    args.legacy_cache_dir=args.base_dir/'data/wrf_platform_cache'
    args.cache_dir=args.base_dir/'data/wrf_platform_cache_10min'
    args.cache_variables=[]
    args.wrfout_dir.mkdir(parents=True,exist_ok=True);args.cache_dir.mkdir(parents=True,exist_ok=True)
    if args.domain!=2 or args.gfs_interval_hours!=1:raise ValueError('Native production requires d02 and hourly GFS forcing')
    return args


def main():
    args=parse_args()
    with production_lock(args.base_dir):
        while True:
            try:once(args)
            except (Exception,SystemExit) as exc:
                print('[NATIVE ERROR] '+str(exc),file=sys.stderr,flush=True)
                if not args.watch:raise
            if not args.watch:break
            print('[NATIVE] next batch check in 600 seconds',flush=True)
            time.sleep(600)


if __name__=='__main__':main()
