"""Replay the captured request against its exact frozen WRF data, read only."""
import argparse
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta
from threading import Event
from time import monotonic

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
sys.path.insert(0,str(BACKEND))
os.environ.update(GFS_AUTO_DOWNLOAD='0',WRF_CACHE_AUTO_SYNC_INDEX='0',WRF_CACHE_REMOTE_HOST='',WRF_NATIVE_10MIN_REQUIRED='1',
    WIND_FRAME_WARMUP='0',ROUTE_PLAN_DIRECTION_PROCESSES='2',ROUTE_PLAN_MAX_TEMPORAL_LABELS='600000',
    ROUTE_PLAN_MEMORY_RESERVE_MB='2048',ROUTE_PLAN_PROCESS_MEMORY_MB='2048',ROUTE_PLAN_WEATHER_LOADING='full',
    ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='2')
from app.models import RoutePlanRequest, FlightProfileRequest
from app.planning_control import planning_scope, PlanningTimedOut
from app.temporal_plan_service import compute_temporal_plan
from app.wrf_timeline import load_dataset
from app.route_quality import assess_route_quality
from app.flight_replay import build_profile
from app.direction_pool import shutdown_direction_pool

WORK=ROOT/'data/route_quality_tests'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--algorithm',default='wa_lpa_star',choices=('astar','lpa_star','wa_lpa_star'))
    parser.add_argument('--offset-hours',type=int,default=0)
    parser.add_argument('--seed-acyclic',action='store_true')
    args=parser.parse_args()
    case=json.loads((BACKEND/'tests/fixtures/route_quality_user_20261115.json').read_text(encoding='utf-8'))
    departure=datetime.fromisoformat(case['departure_at'].replace('Z','+00:00'))+timedelta(hours=args.offset_hours)
    data=load_dataset(case['level'],departure,case['cycle'])
    assert data.dataset_id==case['dataset_id'],'captured forecast version no longer available'
    seeds=[]
    if args.seed_acyclic:
        seeds=[json.loads((WORK/'retiming-audit.json').read_text(encoding='utf-8'))['removed_cycles']['points']]
    req=RoutePlanRequest(source=case['source'],level=case['level'],cycle=case['cycle'],start=case['points'][0],end=case['points'][-1],
        departure_at=departure,time_aware=True,average_ground_speed_mps=case['average_ground_speed_mps'],
        planner_type=args.algorithm,planning_strategy=case['planning_strategy'],prefer_simple_route=True,
        route_quality_extra_seconds=10,seed_routes=seeds)
    published=[];events=[];began=monotonic();status='completed';result=None
    def publish(value):
        assert value['analysis']['navigation_allowed'] and value['display_quality']['acceptable']
        published.append(dict(seconds=monotonic()-began,distance_km=value['distance_km'],quality=value['display_quality']))
    try:
        try:
            with planning_scope(began+70,Event(),progress=lambda value:events.append(dict(seconds=monotonic()-began,**value))):
                result=compute_temporal_plan(req,data,[-180,-90,180,90],0,publish)
        except PlanningTimedOut:
            status='test_budget_reached'
        if result and result.get('points'):
            assert assess_route_quality(result['points'],req.average_ground_speed_mps)['acceptable']
            check=build_profile(result['route_context'],FlightProfileRequest(departure_at=departure,
                average_ground_speed_mps=req.average_ground_speed_mps,source=req.source,thresholds=req.thresholds,wind_shear=req.wind_shear),data)
            assert check['analysis']['navigation_allowed']
        elif result:
            assert result['route_status'] in {'quality_unconfirmed','resource_limit','data_insufficient','no_feasible_route'}
        report=dict(source_job_id=case['job_id'],algorithm=args.algorithm,departure_at=departure.isoformat(),cycle=case['cycle'],
            dataset_id=data.dataset_id,seeded=args.seed_acyclic,status=status,route_status=(result or {}).get('route_status'),
            elapsed_seconds=monotonic()-began,published=published,quality_events=[row for row in events if 'route_quality' in row],
            result=result,writes_history=False)
        path=WORK/f'real-quality-{args.algorithm}-offset{args.offset_hours}.json'
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({**{k:v for k,v in report.items() if k not in ('result','quality_events','published')},
            'published_routes':len(published),'quality_events':[[e['seconds'],e['route_quality']['status']] for e in report['quality_events']]},ensure_ascii=True))
    finally:shutdown_direction_pool()


if __name__=='__main__':main()
