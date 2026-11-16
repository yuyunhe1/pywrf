from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

from netCDF4 import Dataset
import numpy as np
import pytest

from app import wrf_cache_provider as provider,route_storage
from app.models import RoutePlanRequest,RouteRecord
from app.wrf_native_schema import validate_index,npz_metadata,NATIVE_WRF_TIME_MODEL as NATIVE

SERVER=Path(__file__).resolve().parents[1]/'server_wrf_10min'
sys.path.insert(0,str(SERVER))
from run_native import validate_raw,publish
import run_native as producer
from native_runner import output_options

BASE=datetime(2026,11,11,tzinfo=timezone.utc)


def raw_file(path,minute):
    path.parent.mkdir(parents=True,exist_ok=True)
    with Dataset(path,'w',format='NETCDF3_CLASSIC') as d:
        for name,size in [('Time',1),('DateStrLen',19),('bottom_top',2),('bottom_top_stag',3),
            ('south_north',2),('south_north_stag',3),('west_east',2),('west_east_stag',3)]:d.createDimension(name,size)
        d.SIMULATION_START_DATE=BASE.strftime('%Y-%m-%d_%H:%M:%S')
        d.START_DATE=d.SIMULATION_START_DATE
        d.DT=15
        stamp=(BASE+timedelta(minutes=minute)).strftime('%Y-%m-%d_%H:%M:%S')
        d.createVariable('Times','S1',('Time','DateStrLen'))[:]=np.asarray([list(stamp)],dtype='S1')
        dims=('Time','south_north','west_east')
        for name,value in [('XLAT',[[30.01,30.01],[30,30]]),('XLONG',[[117,117.01],[117,117.01]]),
                           ('HGT',0),('COSALPHA',1),('SINALPHA',0),('U10',1+minute/10),('V10',2)]:
            d.createVariable(name,'f4',dims)[:]=value
        d.createVariable('U','f4',('Time','bottom_top','south_north','west_east_stag'))[:]=4+minute/10
        d.createVariable('V','f4',('Time','bottom_top','south_north_stag','west_east'))[:]=3
        dims=('Time','bottom_top_stag','south_north','west_east')
        d.createVariable('PH','f4',dims)[:]=0
        d.createVariable('PHB','f4',dims)[:]=np.asarray([0,100,300])[None,:,None,None]*9.81
        for name,value in [('T',0),('P',0),('PB',100000),('QVAPOR',.005)]:
            d.createVariable(name,'f4',('Time','bottom_top','south_north','west_east'))[:]=value
    return path


@pytest.fixture
def published(tmp_path):
    args=SimpleNamespace(forecast_hours=1,domain=2,heights=(10,100),output_spacing_deg=None,
        cache_dir=tmp_path/'remote',legacy_cache_dir=tmp_path/'legacy',cache_variables=[])
    raw={minute:raw_file(tmp_path/'raw'/('wrfout_d02_'+(BASE+timedelta(minutes=minute)).strftime('%Y-%m-%d_%H-%M-%S')),minute)
         for minute in range(0,61,10)}
    rows=publish(BASE,raw,args)
    return args,raw,rows


def test_frozen_upstream_hashes_and_output_only_overrides():
    manifest=json.loads((SERVER/'upstream/SOURCE.json').read_text(encoding='utf-8'))
    for name,record in manifest['files'].items():
        assert hashlib.sha256((SERVER/'upstream'/name).read_bytes()).hexdigest()==record['sha256']
    assert output_options()=={'time_step':'45','history_interval':'60,10','frames_per_outfile':'1,1'}
    original=(SERVER/'upstream/run_realtime_wrf_platform_cache.py').read_text(encoding='utf-8')
    adapted=(SERVER/'exporter.py').read_text(encoding='utf-8')
    section=lambda s:s[s.index('            u_stag ='):s.index('            rel =',s.index('            u_stag ='))]
    assert section(original)==section(adapted)  # Wind/height derivation unchanged.


def test_latest_cycle_requires_the_full_forecast_tail_before_running_wrf(monkeypatch,tmp_path):
    args=SimpleNamespace(now=BASE,delay_hours=0,cycle_fallback_count=2,gfs_dir=tmp_path,min_bytes=1,
        forecast_hours=48,gfs_interval_hours=1,gfs_vars=None,gfs_levels=None,timeout=900)
    prior=BASE-timedelta(hours=6)
    monkeypatch.setattr(producer.legacy,'is_valid_file',lambda path,minimum:prior.strftime('%Y%m%d%H') in str(path))
    monkeypatch.setattr(producer.legacy,'probe_cycle',lambda *a:True)
    monkeypatch.setattr(producer.legacy,'download',lambda *a:False)
    assert producer.choose_available_cycle(args)==prior


def test_export_keeps_every_minute_and_legacy_whole_hour_view(published):
    from validate_batch import reference_uv,validate_product
    args,raw,rows=published
    assert set(validate_raw(args.cache_dir.parent/'raw',BASE,1))==set(range(0,61,10))
    index=json.loads((args.cache_dir/'index.json').read_text(encoding='utf-8'))
    assert validate_index(index)=={BASE.strftime('%Y-%m-%d %H:%M UTC'):set(range(0,61,10))}
    assert len({r['path'] for r in rows})==7
    for row in rows:
        with np.load(args.cache_dir/row['path']) as data:
            assert float(data['u'][1,0,0])==pytest.approx(4+row['forecast_minutes']/10)
            assert str(data['source_wrf_sha256'].item())==hashlib.sha256(raw[row['forecast_minutes']].read_bytes()).hexdigest()
            with producer.legacy.open_wrf_dataset(raw[row['forecast_minutes']]) as dataset:
                u,v=reference_uv(dataset,data['levels_m'])
                assert np.array_equal(u,data['u'],equal_nan=True) and np.array_equal(v,data['v'],equal_nan=True)
        assert validate_product((args.cache_dir,row,raw[row['forecast_minutes']]))['minute']==row['forecast_minutes']
    legacy=json.loads((args.legacy_cache_dir/'index.json').read_text(encoding='utf-8'))
    assert len(legacy['files'])==1 and legacy['files'][0]['forecast_hour']==1
    publish(BASE,raw,args)  # Recover/retry without overwriting immutable geometry.
    assert len(json.loads((args.cache_dir/'index.json').read_text(encoding='utf-8'))['files'])==7


class SFTP:
    def __init__(self,fail=None):self.downloads=0;self.fail=fail;self.closed=False
    def open(self,path,mode):return open(path,mode)
    def stat(self,path):
        stat=Path(path).stat();return SimpleNamespace(st_size=stat.st_size,st_mtime=stat.st_mtime)
    def get(self,source,target):
        self.downloads+=1
        if self.downloads==self.fail:raise OSError('isolated interrupted transfer')
        shutil.copyfile(source,target)
    def close(self):self.closed=True


def setup_sync(monkeypatch,published,tmp_path,fail=None):
    args,_,_=published
    monkeypatch.setenv('WRF_NATIVE_10MIN_REQUIRED','1')
    monkeypatch.setenv('WRF_CACHE_DIR',str(tmp_path/'local'))
    monkeypatch.setenv('WRF_CACHE_REMOTE_HOST','isolated')
    monkeypatch.setenv('WRF_CACHE_REMOTE_DIR',str(args.cache_dir))
    monkeypatch.setattr(provider,'_remote_path',lambda path:str(args.cache_dir/path))
    connections=[]
    def connect():
        sftp=SFTP(fail);connections.append(sftp)
        return SimpleNamespace(close=lambda:None),sftp
    monkeypatch.setattr(provider,'_connect_sftp',connect)
    provider.load_index.cache_clear();provider._get_grid_cached.cache_clear()
    return args,connections


def test_sync_uses_one_connection_and_publishes_only_verified_complete_batch(monkeypatch,published,tmp_path):
    _,connections=setup_sync(monkeypatch,published,tmp_path)
    result=provider.sync_latest_cycle()
    assert result['downloaded']==7 and len(connections)==1 and connections[0].closed
    assert len(provider.load_index()['files'])==7
    grid=provider.get_grid(BASE.strftime('%Y-%m-%d %H:%M UTC'),1/6,'100m AGL',None)
    assert grid.forecast_minutes==10 and grid.wind_time_model==NATIVE
    second=provider.sync_latest_cycle()
    assert second['skipped']==7 and connections[1].downloads==0
    before=provider._get_grid_cached.cache_stats()
    assert provider.get_grid(BASE.strftime('%Y-%m-%d %H:%M UTC'),1/6,'100m AGL',None) is grid
    assert provider._get_grid_cached.cache_stats()['hits']==before['hits']+1


def test_interrupted_or_corrupt_sync_does_not_replace_the_previous_index(monkeypatch,published,tmp_path):
    args,connections=setup_sync(monkeypatch,published,tmp_path,fail=3)
    local=provider.cache_dir();local.mkdir();(local/'index.json').write_text('previous immutable index')
    with pytest.raises(OSError):provider.sync_latest_cycle()
    assert (local/'index.json').read_text(encoding='utf-8')=='previous immutable index'
    assert connections[0].closed and not list(local.rglob('*.part'))
    # A missing frame cannot be converted into an apparently complete batch.
    index=json.loads((args.cache_dir/'index.json').read_text(encoding='utf-8'));index['files'].pop(1)
    (args.cache_dir/'index.json').write_text(json.dumps(index))
    with pytest.raises(ValueError,match='不完整'):provider.sync_latest_cycle()
    assert (local/'index.json').read_text(encoding='utf-8')=='previous immutable index'


def test_existing_unpublished_older_files_do_not_become_a_verified_batch(monkeypatch,published,tmp_path):
    args,_=setup_sync(monkeypatch,published,tmp_path)
    index=json.loads((args.cache_dir/'index.json').read_text(encoding='utf-8'))
    older=[]
    for row in index['files']:
        copy=dict(row,cycle='2026-11-10 18:00 UTC',path='2026111018/'+Path(row['path']).name)
        stamp=BASE-timedelta(hours=6)+timedelta(minutes=row['forecast_minutes'])
        copy['valid_time']=stamp.strftime('%Y-%m-%d %H:%M UTC')
        # A complete-looking directory has never passed metadata validation or
        # atomic publication. Its existence cannot establish provenance.
        target=provider.cache_dir()/copy['path'];target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(args.cache_dir/row['path'],target)
        older.append(copy)
    index['files'].extend(older)
    (args.cache_dir/'index.json').write_text(json.dumps(index))
    result=provider.sync_latest_cycle()
    assert result['files']==7
    assert provider.load_index()['cycles']==[BASE.strftime('%Y-%m-%d %H:%M UTC')]


def test_strict_mode_rejects_hourly_metadata_and_v3_new_plans(monkeypatch,tmp_path):
    monkeypatch.setenv('WRF_NATIVE_10MIN_REQUIRED','1')
    p=tmp_path/'hour.npz';np.savez(p,forecast_hour=1,cycle_utc='2026-11-11 00:00 UTC',valid_time_utc='2026-11-11 01:00 UTC')
    with np.load(p) as data:
        with pytest.raises(ValueError,match='10 分钟'):npz_metadata(data)
    from app.v3_service import prepare_plan
    with pytest.raises(ValueError,match='暂停新规划'):prepare_plan(None)


def test_minute_request_and_sqlite_roundtrip_keep_fractional_hour(monkeypatch,tmp_path):
    monkeypatch.setattr(route_storage,'DB_PATH',tmp_path/'isolated.sqlite3')
    req=RoutePlanRequest(start=[117,30],end=[117.01,30],source='wrf',level='100m AGL',
        cycle='2026-11-11 00:00 UTC',forecast_minutes=10)
    assert req.forecast_hour==1/6
    record=RouteRecord(name='minute',start=req.start,end=req.end,points=[req.start,req.end],source='wrf',
        level=req.level,cycle=req.cycle,forecast_hour=req.forecast_hour,forecast_minutes=10,valid_time='2026-11-11 00:10 UTC')
    saved=route_storage.save_route(record.model_dump())
    assert saved['forecast_hour']==1/6 and saved['forecast_minutes']==10 and saved['valid_time']=='2026-11-11 00:10 UTC'


@pytest.mark.parametrize('minute',['invalid',None,float('nan'),10.5])
def test_malformed_minute_selection_is_a_validation_error(minute):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RoutePlanRequest(start=[117,30],end=[117.01,30],source='wrf',level='100m AGL',
            cycle='2026-11-11 00:00 UTC',forecast_minutes=minute)


def test_weather_query_accepts_forecast_minutes_without_truncation(monkeypatch):
    from app import main
    from fastapi.testclient import TestClient
    from test_native_wrf_time import native_dataset
    data=native_dataset();seen=[]
    def grid(cycle,hour,level,bbox,valid_time,source):
        seen.append(hour)
        return data.frames[1][1]
    monkeypatch.setattr(main,'load_grid',grid)
    response=TestClient(main.app).get('/api/wind',params=dict(source='wrf',cycle=data.cycle,level='100m AGL',forecast_minutes=10))
    assert response.status_code==200 and seen==[1/6]
    assert response.json()['metadata']['forecast_minutes']==10
    response=TestClient(main.app).get('/api/wind',params=dict(source='wrf',cycle=data.cycle,forecast_minutes=10,forecast_hour=1))
    assert response.status_code==400


def test_page_forecast_revision_includes_native_model_and_product_hash(monkeypatch,tmp_path):
    from app import main
    product=tmp_path/'frame.npz';product.write_bytes(b'unchanged bytes')
    index=dict(wind_time_model=NATIVE,files=[dict(path=product.name,sha256='a'*64)])
    monkeypatch.setattr(main,'availability',lambda *a,**k:{})
    monkeypatch.setattr(main.v3_native,'status',lambda:{})
    monkeypatch.setattr(provider,'cache_dir',lambda:tmp_path)
    monkeypatch.setattr(provider,'load_index',lambda:index)
    def revision():return main.times(refresh_files=False,auto_download=False,source='wrf')['forecast_revision']
    before=revision()
    assert before==revision()
    index['files'][0]['sha256']='b'*64
    after=revision()
    assert after!=before
    index['wind_time_model']='OTHER_MODEL'
    assert revision()!=after


def test_original_cli_delegation_preserves_all_imported_function_definitions():
    import ast
    from legacy_entry import guarded_source
    original=(SERVER/'upstream/run_realtime_wrf_platform_cache.py').read_bytes()
    guarded=guarded_source(original)
    functions=lambda raw:{n.name:ast.dump(n,include_attributes=False) for n in ast.parse(raw).body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
    assert functions(original)==functions(guarded)
    assert 'run_name="__main__"' in guarded.decode('utf-8')
