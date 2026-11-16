"""Read-only saved-route audit and real-WRF planning under closed-cell risk."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from threading import Event
from time import monotonic
from unittest.mock import patch

import numpy as np

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND))
from verify_segmented_planning import pinned_dataset, ROOT
from app.departure_windows import CellVisit, prepare_evaluation, describe_evaluation
from app.direction_pool import warm_direction_pool, shutdown_direction_pool
from app.flight_replay import build_profile
from app.grid_coverage import cell_visits, SPATIAL_RISK_MODEL
from app.local_weather import LocalWeather
from app.models import FlightProfileRequest, RoutePlanRequest
from app.planning_control import planning_scope, PlanningTimedOut
from app.planning_objective import wind_preference, OBJECTIVE_MODEL
from app.route_options import build_strategy_cost_config
from app.route_service import haversine_km
from app.temporal_plan_service import compute_temporal_plan
from app.wind_time import frame_boundaries

WORK=ROOT/'data/grid_contact_planning_tests'


def legacy_visits(points,grid,speed):
    """Frozen prior nearest-midpoint traversal, only for this isolated audit."""
    result=[]
    elapsed=0.
    edges=((grid.lons[1:]+grid.lons[:-1])/2,(grid.lats[1:]+grid.lats[:-1])/2)
    def add(a,b,enter,leave):
        row=int(np.abs(grid.lats-(a[1]+b[1])/2).argmin())
        col=int(np.abs(grid.lons-(a[0]+b[0])/2).argmin())
        if result and (result[-1].row,result[-1].col,result[-1].leave)==(row,col,enter):
            old=result[-1]
            result[-1]=CellVisit(row,col,old.enter,leave,old.start_point,b)
        else:
            result.append(CellVisit(row,col,enter,leave,a,b))
    for a,b in zip(points,points[1:]):
        duration=haversine_km(a,b)*1000/speed
        cuts={0.,1.}
        for dim,values in enumerate(edges):
            delta=b[dim]-a[dim]
            if delta:
                lo,hi=sorted((a[dim],b[dim]))
                cuts.update(float((v-a[dim])/delta) for v in values[(values>lo)&(values<hi)])
        cuts=sorted(cuts)
        at=lambda f:(a[0]+(b[0]-a[0])*f,a[1]+(b[1]-a[1])*f)
        for index,cut in enumerate(cuts):
            p=at(cut);offset=elapsed+duration*cut
            add(p,p,offset,offset)
            if index+1<len(cuts):
                nxt=cuts[index+1]
                add(p,at(nxt),offset,elapsed+duration*nxt)
        elapsed+=duration
    return result


def legacy_cost(points,w):
    stamp=w.request.departure_at.timestamp()
    total=0.
    for a,b in zip(points,points[1:]):
        distance=haversine_km(a,b);duration=distance*1000/w.speed
        if not duration: continue
        heading=math.atan2((b[0]-a[0])*math.cos(math.radians((a[1]+b[1])/2)),b[1]-a[1])
        east,north=math.sin(heading),math.cos(heading)
        value=w.config.weights.alpha_distance*distance
        for visit in legacy_visits([a,b],w.grid,w.speed):
            cuts=[visit.enter,*[t-stamp for t in frame_boundaries(stamp+visit.enter,stamp+visit.leave)],visit.leave]
            for left,right in zip(cuts,cuts[1:]):
                if right<=left: continue
                vector=w.vector(stamp+(left+right)/2,visit.row,visit.col)
                if vector is None: return None
                u,v=vector
                value+=distance*(right-left)/duration*wind_preference(math.hypot(u,v),u*east+v*north,
                    abs(u*north-v*east),w.config.weights,w.config.thresholds)
        total+=value;stamp+=duration
    return total


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',choices=['saved','first','original'],default='saved')
    parser.add_argument('--budget',type=float,default=90)
    parser.add_argument('--comparison-seconds',type=int,default=2)
    args=parser.parse_args()
    os.environ.update(ROUTE_PLAN_WEATHER_LOADING='full',ROUTE_PLAN_DIRECTION_PROCESSES='2',
        ROUTE_PLAN_MAX_TEMPORAL_LABELS='0',ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS=str(args.comparison_seconds))
    WORK.mkdir(parents=True,exist_ok=True)
    report=dict(case=args.case,writes_platform_history=False,spatial_risk_model=SPATIAL_RISK_MODEL,
                objective_model=OBJECTIVE_MODEL,comparison_seconds=args.comparison_seconds,budget=args.budget)
    if args.case=='saved':
        with sqlite3.connect((BACKEND/'routes.sqlite3').as_uri()+'?mode=ro',uri=True) as connection:
            connection.row_factory=sqlite3.Row
            row=dict(connection.execute('SELECT * FROM routes ORDER BY created_at DESC LIMIT 1').fetchone())
        points=[p[:2] for p in json.loads(row['points_json'])]
        start,end,cycle=points[0],points[-1],row['cycle']
        departure=datetime.fromisoformat(row['created_at'])
        report['saved_record']=dict(route_id=row['route_id'],name=row['name'],
            departure_basis='save timestamp proxy; planned departure is not stored in the route table',
            displayed_forecast_hour=row['forecast_hour'])
    else:
        if args.case=='first':
            start,end,cycle,departure=[119.56970,31.22689],[119.96521,30.55753],'2026-11-10 00:00 UTC','2026-11-10T08:37:04.845Z'
            points=json.loads((ROOT/'data/route_refinement_tests/first-original-reference.json').read_text(encoding='utf-8'))['points']
        else:
            start,end,cycle,departure=[117.17468,30.91165],[118.13049,30.39657],'2026-11-04 00:00 UTC','2026-11-04T09:00:00Z'
            points=json.loads((ROOT/'data/cross_time_avoidance_tests/classic-user-case-full-lpa_star.json').read_text(encoding='utf-8'))['points']
    data=pinned_dataset(cycle,'100m AGL')
    report.update(dataset_id=data.dataset_id,source_hashes=data.source_hashes)
    request=RoutePlanRequest(start=start,end=end,source='wrf',level='100m AGL',time_aware=True,
        departure_at=departure,average_ground_speed_mps=10,planning_strategy='wind_avoidance',planner_type='wa_lpa_star')
    w=LocalWeather(data,request,build_strategy_cost_config(request.planning_strategy))
    points=[p[:2] for p in points]
    old=legacy_visits(points,w.grid,10)
    new=cell_visits(points,w.grid,10)
    missed=[]
    for visit in new:
        if visit.enter==visit.leave and not any((v.row,v.col)==(visit.row,visit.col) and v.enter<=visit.enter<=v.leave for v in old):
            vector=w.vector(request.departure_at.timestamp()+visit.enter,visit.row,visit.col)
            missed.append(dict(row=visit.row,col=visit.col,seconds=visit.enter,point=visit.start_point,
                               wind_speed=math.hypot(*vector) if vector else None))
    # Wind-only before/after is isolated from the independently strengthened
    # corner-shear check. No production function or saved record is replaced.
    with patch('app.departure_windows._cell_visits',legacy_visits):
        prior=prepare_evaluation(points,list(data.frames),10,7.9,None)
    strict=prepare_evaluation(points,list(data.frames),10,7.9,None)
    flight=build_profile(dict(route_id='reference',name='reference',points=points,level=request.level),
        FlightProfileRequest(departure_at=request.departure_at,average_ground_speed_mps=10),data)
    report['reference']=dict(missed_point_contacts=missed,
        old_wind_decision=describe_evaluation(prior,request.departure_at,10)['current_departure'],
        new_wind_decision=describe_evaluation(strict,request.departure_at,10)['current_departure'],
        full_decision=flight['decision'],old_soft_cost=legacy_cost(points,w),
        new_soft_cost=w.route_cost(points,require_allowed=False),max_wind_speed=flight['analysis']['max_wind_speed'])
    warm_direction_pool()
    began=monotonic();publications=[]
    def publish(value):
        assert value['analysis']['navigation_allowed']
        publications.append(dict(seconds=monotonic()-began,km=value['distance_km'],cost=value['objective_cost']))
        (WORK/f'{args.case}-plan.json').write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    result=None
    try:
        with planning_scope(monotonic()+args.budget,Event()):
            result=compute_temporal_plan(request,data,[116.9,29.7,120.2,32.4],0,publish)
    except PlanningTimedOut:
        report['status']='test_budget_expired'
    finally:
        shutdown_direction_pool()
    report.update(seconds=monotonic()-began,publications=publications,
                  diagnostics=result.get('planning_diagnostics') if result else None)
    (WORK/f'{args.case}-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='diagnostics'},ensure_ascii=False))
    assert publications, result and result.get('planning_message')


if __name__=='__main__':
    main()
