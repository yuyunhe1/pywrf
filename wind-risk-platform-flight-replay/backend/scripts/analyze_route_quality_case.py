"""Retiming audit of the captured user case; never writes route history."""
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
sys.path.insert(0,str(BACKEND))
os.environ.update(WRF_NATIVE_10MIN_REQUIRED='1',GFS_AUTO_DOWNLOAD='0',WRF_CACHE_AUTO_SYNC_INDEX='0',
    WRF_CACHE_REMOTE_HOST='',WIND_FRAME_WARMUP='0',ROUTE_PLAN_WEATHER_LOADING='full')
from app.models import RoutePlanRequest, FlightProfileRequest
from app.route_refinement import cycle_proposals, RouteRefiner, shape_quality
from app.route_options import build_strategy_cost_config
from app.route_service import haversine_km
from app.wrf_timeline import load_dataset
from app.local_weather import LocalWeather
from app.flight_replay import build_profile
from app.route_context import geometry_route

WORK=ROOT/'data/route_quality_tests'
IDENTITY='d8a004b7-ddc3-42f4-b486-5d742d47bb69'


def main():
    result=json.loads((WORK/f'captured-{IDENTITY}.json').read_text(encoding='utf-8'))['result']
    departure=datetime.fromisoformat(result['departure_at'].replace('Z','+00:00'))
    req=RoutePlanRequest(source=result['source'],level=result['level'],start=result['points'][0],end=result['points'][-1],
        cycle=result['metadata']['cycle'],departure_at=departure,time_aware=True,
        average_ground_speed_mps=result['average_ground_speed_mps'],planning_strategy=result['planning_strategy'],
        planner_type=result['planner_type'])
    data=load_dataset(req.level,departure,req.cycle)
    assert data.dataset_id==result['dataset_id'], 'captured immutable weather version unavailable'
    weather=LocalWeather(data,req,build_strategy_cost_config(req.planning_strategy))
    points=result['points'];began=monotonic()
    proposals=cycle_proposals(points)
    safe=[p for p in proposals if isfinite(weather.route_cost(p))]
    refined,cost,stats=RouteRefiner(weather,points).finish()
    acyclic=points
    while (proposals_left:=cycle_proposals(acyclic)):
        acyclic=proposals_left[0]
    length=lambda p:sum(haversine_km(a,b) for a,b in zip(p,p[1:]))
    def check(points,departure):
        profile=build_profile(geometry_route(points,req.level,source=req.source),FlightProfileRequest(
            departure_at=departure,source=req.source,average_ground_speed_mps=req.average_ground_speed_mps,
            thresholds=req.thresholds,wind_shear=req.wind_shear),data)
        return {k:profile['analysis'].get(k) for k in ('navigation_allowed','navigation_decision_reason','max_wind_speed','mean_wind_speed','total_distance_km')}
    report=dict(job_id=IDENTITY,cycle=req.cycle,dataset_id=data.dataset_id,departure_at=req.departure_at.isoformat(),
        start=req.start,end=req.end,original=dict(distance_km=length(points),shape=shape_quality(points),analysis=check(points,departure)),
        cycle_count=len(proposals),safe_single_cycle_deletions=len(safe),
        refiner=dict(distance_km=length(refined),shape=shape_quality(refined),statistics=stats),
        removed_cycles=dict(distance_km=length(acyclic),shape=shape_quality(acyclic),points=acyclic,
            future_checks=[dict(offset_hours=i,**check(acyclic,departure+timedelta(hours=i))) for i in range(4)]),
        direct=check([req.start,req.end],departure),elapsed_seconds=monotonic()-began,writes_history=False)
    (WORK/'retiming-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v if k!='removed_cycles' else {a:b for a,b in v.items() if a!='points'} for k,v in report.items()},ensure_ascii=True,indent=2))


if __name__=='__main__':main()
