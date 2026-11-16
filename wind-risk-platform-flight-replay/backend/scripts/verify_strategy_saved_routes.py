"""Read saved geometry as cases; never update history or the normal service clock."""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic
from urllib.request import build_opener,ProxyHandler

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
sys.path.insert(0,str(BACKEND))
sys.stdout.reconfigure(encoding='utf-8')
os.environ.update(GFS_AUTO_DOWNLOAD='0',WRF_CACHE_AUTO_SYNC_INDEX='0',WRF_CACHE_REMOTE_HOST='',WRF_V3_AUTO_SYNC='0',
    ROUTE_PLAN_DIRECTION_PROCESSES='2',ROUTE_PLAN_DEPARTURE_CONCURRENCY='2',ROUTE_PLAN_WEATHER_LOADING='full',
    ROUTE_PLAN_SEARCH_FULL_DOMAIN='1',ROUTE_PLAN_MAX_TEMPORAL_LABELS='0',ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='0')
from app.models import RoutePlanRequest
from app.planning_control import planning_scope,PlanningTimedOut
from app.temporal_plan_service import compute_temporal_plan
from app.wrf_timeline import load_dataset
from app.direction_pool import shutdown_direction_pool

WORK=ROOT/'data/strategy_saved_tests'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--phase',required=True)
    parser.add_argument('--seconds',type=float,default=30)
    parser.add_argument('--departure',help='An explicit zoned test departure; default is saved creation minute')
    parser.add_argument('--route-id')
    args=parser.parse_args()
    WORK.mkdir(parents=True,exist_ok=True)
    path=WORK/'saved-cases.json'
    if not path.is_file():
        rows=json.load(build_opener(ProxyHandler({})).open('http://127.0.0.1:8001/api/routes'))
        path.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    rows=json.loads(path.read_text(encoding='utf-8'))
    try:
        for row in rows:
            if args.route_id and row['route_id']!=args.route_id: continue
            at=datetime.fromisoformat(args.departure or row['created_at']).replace(second=0,microsecond=0)
            data=load_dataset(row['level'],at,row['cycle'])
            for strategy in ('distance_priority','wind_avoidance'):
                req=RoutePlanRequest(start=row['start'],end=row['end'],source=row['source'],level=row['level'],
                    cycle=row['cycle'],departure_at=at,average_ground_speed_mps=10,time_aware=True,
                    planner_type='wa_lpa_star',planning_strategy=strategy)
                published=[];best=None;stats={}
                began=monotonic()
                def publish(value):
                    nonlocal best
                    best=deepcopy(value)
                    published.append(dict(seconds=monotonic()-began,cost=value['objective_cost'],
                        distance_km=value['distance_km'],mean_wind=value['analysis']['mean_wind_speed'],
                        max_wind=value['analysis']['max_wind_speed']))
                def progress(value): stats.update(value.get('statistics',{}))
                status='completed'
                try:
                    with planning_scope(began+args.seconds,Event(),progress=progress):
                        result=compute_temporal_plan(req,data,[-180,-90,180,90],0,publish)
                        if result.get('analysis',{}).get('navigation_allowed'): best=result
                except PlanningTimedOut: status='budget_reached'
                except RuntimeError as exc:
                    if str(exc)!='PlanningTimedOut': raise
                    status='budget_reached'
                summary=dict(phase=args.phase,route_id=row['route_id'],name=row['name'],departure_at=at.isoformat(),
                    strategy=strategy,seconds=monotonic()-began,status=status,published=published,
                    distance_km=best['distance_km'] if best else None,
                    mean_wind=best['analysis']['mean_wind_speed'] if best else None,
                    max_wind=best['analysis']['max_wind_speed'] if best else None,
                    dataset_id=data.dataset_id,statistics=stats,writes_history=False,result=best)
                target=WORK/f'{args.phase}-{row["route_id"][:8]}-{strategy}.json'
                target.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({k:v for k,v in summary.items() if k not in ('result','statistics')},ensure_ascii=False),flush=True)
    finally: shutdown_direction_pool()


if __name__=='__main__': main()
