"""Synchronize and benchmark real native products without writing route history."""
import argparse
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND))
WORK=BACKEND.parents[1]/'data/native_wrf_10min_tests'
os.environ.update(WRF_NATIVE_10MIN_REQUIRED='1',WRF_CACHE_AUTO_SYNC_INDEX='0',GFS_AUTO_DOWNLOAD='0',
    WRF_V3_AUTO_SYNC='0',ROUTE_PLAN_DIRECTION_PROCESSES='2',ROUTE_PLAN_WEATHER_LOADING='full',
    ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='2',ROUTE_PLAN_MAX_TEMPORAL_LABELS='0')
os.environ.setdefault('WRF_CACHE_DIR',str(BACKEND.parent/'data/wrf_platform_cache_10min'))
os.environ.setdefault('WRF_CACHE_REMOTE_HOST','10.129.59.14')
os.environ.setdefault('WRF_CACHE_REMOTE_USER','root')
os.environ.setdefault('WRF_CACHE_REMOTE_DIR','/root/pyWRF-automation/data/wrf_platform_cache_10min')

import numpy as np
from app import wrf_cache_provider as provider
from app.wrf_timeline import load_dataset,bracket,vector_at,wind_frame
from app.wrf_native_schema import NATIVE_WRF_TIME_MODEL as NATIVE
from app.models import RoutePlanRequest,FlightProfileRequest
from app.local_weather import LocalWeather
from app.route_options import build_strategy_cost_config
from app.route_context import geometry_route
from app.flight_replay import build_profile
from app.temporal_plan_service import compute_temporal_plan
from app.planning_control import planning_scope,PlanningTimedOut
from app.direction_pool import shutdown_direction_pool
from app.planning_limits import process_peak_memory_mb


def hold_proof(data):
    a,b=data.frames[:2];u,v=np.asarray(a[1].u),np.asarray(a[1].v)
    du,dv=np.asarray(b[1].u)-u,np.asarray(b[1].v)-v
    change=np.hypot(du,dv);change[~np.isfinite(change)]=-1
    row,col=np.unravel_index(np.argmax(change),change.shape)
    point=float(a[1].lons[col]),float(a[1].lats[row])
    before=vector_at(data,a[0],*point);inside=vector_at(data,a[0]+180,*point);next_frame=vector_at(data,b[0],*point)
    assert inside==before and next_frame==vector_at(data,a[0]+600,*point)
    hypothetical=tuple(x+.3*(y-x) for x,y in zip(before,next_frame))
    assert change[row,col]>0 and inside!=hypothetical
    return dict(point=point,at_start=before,at_three_minutes=inside,at_ten_minutes=next_frame,
        hypothetical_linear= hypothetical,raw_vector_change_mps=float(change[row,col]),wind_time_model=data.time_model)


def short_request(data,departure):
    grid=bracket(data,departure.timestamp())[0]
    winds=np.hypot(np.asarray(grid.u),np.asarray(grid.v));winds[:2]=winds[-2:]=np.inf;winds[:,:2]=winds[:,-2:]=np.inf
    config=dict(level=data.level,source='wrf',cycle=data.cycle,time_aware=True,departure_at=departure,
        average_ground_speed_mps=10,planning_strategy='wind_avoidance')
    for index in np.argsort(winds,axis=None)[:30]:
        row,col=np.unravel_index(index,winds.shape)
        if not np.isfinite(winds[row,col]) or winds[row,col]>=7.9:continue
        lon,lat=float(grid.lons[col]),float(grid.lats[row])
        req=RoutePlanRequest(start=[lon-.0045,lat],end=[lon+.0045,lat],**config)
        route=geometry_route([req.start,req.end],data.level,source='wrf')
        profile=build_profile(route,FlightProfileRequest(**req.model_dump(include={'departure_at','average_ground_speed_mps','thresholds','wind_shear','source'})),data)
        if profile['analysis']['navigation_allowed']:return req
    raise AssertionError('No independently verified short benchmark route found')


def benchmark(req,data,seconds):
    began=monotonic();accepted=[];result=None;retained=None;progress=None
    def publish(value):
        nonlocal retained
        retained=value
        accepted.append(dict(seconds=monotonic()-began,distance_km=value['distance_km'],
            objective_cost=value['objective_cost'],wind_time_model=value['wind_time_model']))
    def observe(value):
        nonlocal progress
        progress=value.get('statistics')
    try:
        with planning_scope(monotonic()+seconds,Event(),progress=observe):
            result=compute_temporal_plan(req,data,[-180,-90,180,90],0,publish)
    except PlanningTimedOut:pass
    result=result or retained
    if result and result.get('analysis',{}).get('navigation_allowed'):
        independently=build_profile(geometry_route(result['points'],req.level,source='wrf'),
            FlightProfileRequest(**req.model_dump(include={'departure_at','average_ground_speed_mps','thresholds','wind_shear','source'})),data)
        assert independently['analysis']['navigation_allowed'] and independently['wind_time_model']==NATIVE
    return dict(algorithm=req.planner_type,start=req.start,end=req.end,departure_at=req.departure_at.isoformat(),
        first_feasible_seconds=accepted[0]['seconds'] if accepted else None,total_seconds=monotonic()-began,
        accepted=accepted,status='verified' if accepted else 'unconfirmed',
        distance_km=result.get('distance_km') if result else None,
        last_observed_search=progress,
        diagnostics=result.get('planning_diagnostics') if result else None)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--sync',action='store_true');parser.add_argument('--performance',action='store_true')
    args=parser.parse_args();WORK.mkdir(parents=True,exist_ok=True)
    report=dict(writes_history=False)
    if args.sync:
        report['sync']=provider.sync_latest_cycle()
        assert report['sync']['files']==289 and report['sync']['wind_time_model']==NATIVE
        (WORK/'local-sync.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)
    if not args.performance:return
    began=monotonic()
    cycle=max(provider.load_index()['cycles'])
    data=load_dataset('100m AGL',datetime.now(timezone.utc),cycle)
    assert len(data.frames)==289 and data.time_model==NATIVE
    report['snapshot_and_metadata_seconds']=monotonic()-began
    departure=datetime.fromtimestamp((int(datetime.now(timezone.utc).timestamp())//60+2)*60,timezone.utc)
    middle=data.frames[0][1]
    req=RoutePlanRequest(start=[float(np.mean(middle.lons)),float(np.mean(middle.lats))],
        end=[float(np.mean(middle.lons))+.01,float(np.mean(middle.lats))],source='wrf',cycle=cycle,level='100m AGL',time_aware=True,departure_at=departure)
    began=monotonic();weather=LocalWeather(data,req,build_strategy_cost_config(req.planning_strategy))
    report['cold_full_fields_seconds']=monotonic()-began
    report['weather']=weather.diagnostics();del weather
    began=monotonic();weather=LocalWeather(data,req,build_strategy_cost_config(req.planning_strategy))
    report['warm_full_fields_seconds']=monotonic()-began;del weather
    report['dataset_id']=data.dataset_id;report['cycle']=cycle;report['hold_proof']=hold_proof(data)
    req=short_request(data,departure)
    report['short_routes']=[benchmark(req.model_copy(update={'planner_type':a}),data,20) for a in ('astar','lpa_star','wa_lpa_star')]
    long=req.model_copy(update={'start':(117.17468,30.91165),'end':(118.13049,30.39657),'planner_type':'wa_lpa_star'})
    report['long_route']=benchmark(long,data,60)
    report['process_peak_memory_mb']=process_peak_memory_mb()
    (WORK/'real-performance.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'short_routes','long_route','weather'}}),flush=True)
    for row in [*report['short_routes'],report['long_route']]:print(json.dumps({k:v for k,v in row.items() if k!='diagnostics'}),flush=True)
    shutdown_direction_pool()


if __name__=='__main__':main()
