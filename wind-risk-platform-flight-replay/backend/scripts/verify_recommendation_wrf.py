"""Read-only Nov 8 forecast comparison; isolated clocks and bounded test optimization."""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
sys.path.insert(0,str(BACKEND))
sys.stdout.reconfigure(encoding='utf-8')
os.environ.update(GFS_AUTO_DOWNLOAD='0',WRF_CACHE_AUTO_SYNC_INDEX='0',WRF_CACHE_REMOTE_HOST='',
    WRF_V3_AUTO_SYNC='0',WRF_CACHE_DIR=str(BACKEND.parent/'data/wrf_platform_cache'),
    ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS='60',ROUTE_PLAN_WEATHER_LOADING='full',
    ROUTE_PLAN_DIRECTION_PROCESSES='2',ROUTE_PLAN_DEPARTURE_CONCURRENCY='2',
    ROUTE_PLAN_SEARCH_FULL_DOMAIN='1',ROUTE_PLAN_MAX_TEMPORAL_LABELS='0',
    ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='2')

from app.recommendations import Workflow
from app.models import RecommendationJobRequest,FlightProfileRequest
from app.wrf_timeline import load_dataset
from app.planning_control import planning_scope,PlanningTimedOut
from app.direction_pool import shutdown_direction_pool
from app.flight_replay import build_profile
from app.route_context import geometry_route
from app.planning_limits import process_peak_memory_mb

WORK=ROOT/'data/recommendation_tests'
CASES=ROOT/'data/route_example_20261108_1600'


def run(name,cycle,hour,budget,detour=False):
    reference=datetime.fromisoformat(f'2026-11-08T{hour:02}:00:00+08:00')
    body=json.loads((CASES/f'{name}-request.json').read_text(encoding='utf-8'))
    body.pop('dataset_id',None)
    body.update(departure_at=reference,cycle=cycle)
    if detour:
        body.update(mode='fixed_route',points=[body['start'],body['end']],segment_index=0,max_detour_ratio=.5)
    request=RecommendationJobRequest(**body)
    began=monotonic()
    data=load_dataset(request.level,reference,cycle)
    preparation=monotonic()-began
    assert data.cycle==cycle and len(data.frames)==48,(cycle,len(data.frames))
    published=[];updates={};stages=[]
    def report(value):
        updates.update(value)
        if 'stage' in value: stages.append({'seconds':monotonic()-began,'stage':value['stage']})
    def publish(value):
        published.append({'seconds':monotonic()-began,'distance_km':value['distance_km'],
            'objective_cost':value['objective_cost'],'points':value['points']})
    status='completed'
    try:
        with planning_scope(monotonic()+budget,Event()):
            engine=Workflow(request,data,report,publish,now=lambda:reference)
            engine.run()
    except PlanningTimedOut: status='test_budget_reached'
    cards=sorted(engine.cards.values(),key=lambda c:c['first_departure_at'])
    for card in cards:
        result=build_profile(geometry_route(card['plan']['points'],request.level,source='wrf'),
            FlightProfileRequest(departure_at=card['first_departure_at'],average_ground_speed_mps=10,
                thresholds=request.thresholds,wind_shear=request.wind_shear),data)
        assert result['analysis']['navigation_allowed'],(card['id'],result['decision'])
    output=dict(case=name,mode=request.mode,cycle=cycle,departure=reference.isoformat(),
        writes_platform_history=False,clock_scope='Workflow.now only; normal service clock unchanged',
        test_budget_seconds=budget,production_parent_seconds=7200,attempt_seconds=60,
        test_improvement_seconds=2,dataset_id=data.dataset_id,source_hashes=data.source_hashes,
        hours=len(data.frames),preparation_seconds=preparation,total_seconds=monotonic()-began,
        status=status,stages=stages,publications=published,workflow=updates,attempts=engine.attempts,
        recommendations=cards,io=data.io_stats.snapshot(),coordinator_peak_mb=process_peak_memory_mb())
    path=WORK/f"wrf-{name}-{cycle[11:13]}utc-{hour}{'-detour' if detour else ''}.json"
    path.write_text(json.dumps(output,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps(dict(case=name,cycle=cycle,hour=hour,mode=request.mode,status=status,
        seconds=round(output['total_seconds'],2),first=published[0]['seconds'] if published else None,
        recommendations=len(cards),attempts=[(a['departure_at'],a['status'],round(a.get('elapsed_seconds',0),2)) for a in engine.attempts],
        path=str(path)),ensure_ascii=False),flush=True)
    return output


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--budget',type=float,default=90)
    parser.add_argument('--cycle',choices=['00','06','both'],default='both')
    parser.add_argument('--case',choices=['north','long','both'],default='both')
    parser.add_argument('--hour',type=int,choices=[15,16,17])
    parser.add_argument('--detour',action='store_true')
    parser.add_argument('--audit-references',action='store_true')
    args=parser.parse_args()
    WORK.mkdir(parents=True,exist_ok=True)
    if args.audit_references:
        audit=[]
        north=json.loads((CASES/'north-plan.json').read_text(encoding='utf-8'))['points']
        long=[]
        for segment in json.loads((CASES/'screened-path-profile.json').read_text(encoding='utf-8'))['segments']:
            for point in segment['points']:
                if not long or point!=long[-1]: long.append(point)
        for cycle in ['06','00']:
            for hour in [15,16,17]:
                at=datetime.fromisoformat(f'2026-11-08T{hour}:00:00+08:00')
                data=load_dataset('100m AGL',at,f'2026-11-08 {cycle}:00 UTC')
                for name,points in [('north',north),('long',long)]:
                    flight=build_profile(geometry_route(points,'100m AGL'),FlightProfileRequest(
                        departure_at=at,average_ground_speed_mps=10),data)
                    audit.append(dict(case=name,cycle=data.cycle,departure=at.isoformat(),dataset_id=data.dataset_id,
                        distance_km=flight['analysis']['total_distance_km'],decision=flight['decision'],
                        max_wind_speed=flight['analysis']['max_wind_speed']))
        (WORK/'real-reference-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(audit,ensure_ascii=False))
        return
    try:
        for cycle in (['06','00'] if args.cycle=='both' else [args.cycle]):
            for name in (['north','long'] if args.case=='both' else [args.case]):
                for hour in ([args.hour] if args.hour else [15,16,17]):
                    run(name,f'2026-11-08 {cycle}:00 UTC',hour,args.budget,args.detour)
    finally: shutdown_direction_pool()


if __name__=='__main__': main()
