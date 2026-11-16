"""Isolated native-minute UI fixtures; real jobs, algorithms and persistence."""
import os
from pathlib import Path
from dataclasses import replace
from datetime import timedelta,datetime,timezone
import json
import hashlib

os.environ.setdefault('CUSTOM_ROUTE_TEST_DIR',str(Path(__file__).resolve().parents[3]/'data/native_wrf_10min_tests/browser-storage'))
os.environ['WRF_NATIVE_10MIN_REQUIRED']='1'
import recommendation_smoke_server as recommendation
fixture=recommendation.fixture
import numpy as np
from app.wrf_native_schema import NATIVE_WRF_TIME_MODEL as NATIVE

for (source,level),data in list(fixture.DATASETS.items()):
    if source!='wrf' or not data.frames:continue
    frames=[]
    for minute in range(0,361,10):
        valid=fixture.BASE+timedelta(minutes=minute)
        u=np.full((21,25),10. if level=='300m AGL' else 12. if level=='200m AGL' and 60<=minute<120 else 1. if level=='200m AGL' else 2.+(minute%60)/100)
        if level=='100m AGL' and minute<=120:u[7:10,11]=12
        grid=replace(data.frames[0][1],u=u,v=np.zeros_like(u),valid_time=valid.strftime('%Y-%m-%d %H:%M UTC'),
            forecast_hour=minute/60,forecast_minutes=minute,wind_time_model=NATIVE)
        frames.append((valid.timestamp(),grid))
    data=replace(data,dataset_id='native-ui-'+level.split('m')[0],frames=tuple(frames))
    fixture.DATASETS[source,level]=data;fixture.wrf_timeline._DATASETS[data.dataset_id]=data

storage=Path(os.environ['CUSTOM_ROUTE_TEST_DIR'])/'weather'
storage.mkdir(parents=True,exist_ok=True)
rows=[]
for stamp,grid in fixture.DATASETS['wrf','100m AGL'].frames:
    p=storage/('minute_%05d.npz'%grid.forecast_minutes)
    np.savez_compressed(p,lons=grid.lons,lats=grid.lats,u=grid.u[None],v=grid.v[None],levels_m=[100],
        cycle_utc=grid.cycle,valid_time_utc=grid.valid_time,forecast_hour=grid.forecast_hour,
        forecast_minutes=grid.forecast_minutes,wind_time_model=NATIVE,output_interval_seconds=600)
    rows.append(dict(cycle=grid.cycle,valid_time=grid.valid_time,forecast_hour=grid.forecast_hour,
        forecast_minutes=grid.forecast_minutes,wind_time_model=NATIVE,output_interval_seconds=600,
        path=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest(),size_bytes=p.stat().st_size))
index=dict(files=rows,output_interval_seconds=600,wind_time_model=NATIVE,forecast_duration_minutes=360)
fixture.main.wrf_cache_provider.cache_dir=lambda:storage
fixture.main.wrf_cache_provider.load_index=lambda:index
old_availability=fixture.main.availability
def availability(source=None,**kwargs):
    result=old_availability(source,**kwargs)
    if source=='wrf':
        result.update(valid_times=[dict(row,label=row['valid_time']) for row in rows],
            forecast_hours=[row['forecast_hour'] for row in rows],wind_time_model=NATIVE,output_interval_seconds=600)
    return result
fixture.main.availability=availability
old_load=fixture.main.load_grid
def load_grid(cycle=None,forecast_hour=None,level='100m AGL',bbox=None,valid_time=None,source=None):
    if source!='wrf':return old_load(cycle,forecast_hour,level,bbox,valid_time,source)
    minute=round(float(forecast_hour or 0)*60)
    if valid_time:
        if '北京时间' in valid_time:
            t=datetime.strptime(valid_time.replace('北京时间','').strip(),'%Y-%m-%d %H:%M').replace(tzinfo=timezone(timedelta(hours=8)))
        else:t=datetime.strptime(valid_time,'%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
        minute=round((t-fixture.BASE).total_seconds()/60)
    data=fixture.DATASETS['wrf',level]
    return next(grid for _,grid in data.frames if grid.forecast_minutes==minute)
fixture.main.load_grid=load_grid

if __name__=='__main__':
    import uvicorn
    uvicorn.run(fixture.main.app,host='127.0.0.1',port=8002)
