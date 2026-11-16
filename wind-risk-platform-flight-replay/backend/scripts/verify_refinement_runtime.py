"""Isolated real-WRF regression; user routes are SELECTed through SQLite mode=ro."""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from threading import Event
from time import monotonic

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / 'scripts'))
from verify_segmented_planning import pinned_dataset, ROOT
from app.flight_replay import build_profile
from app.local_weather import LocalWeather
from app.models import RoutePlanRequest, FlightProfileRequest
from app.planning_control import planning_scope, PlanningTimedOut
from app.route_options import build_strategy_cost_config
from app.route_refinement import RouteRefiner, cycle_proposals, shape_quality
from app.temporal_plan_service import compute_temporal_plan
from app.direction_pool import shutdown_direction_pool, warm_direction_pool

WORK = ROOT / 'data/route_refinement_tests'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['anomaly','first','short','saved','hard','original'], default='anomaly')
    parser.add_argument('--mode', choices=['parallel','serial'], default='parallel')
    parser.add_argument('--budget', type=float, default=25)
    parser.add_argument('--comparison-seconds', type=int, default=2)
    parser.add_argument('--cold-workers', action='store_true')
    args = parser.parse_args()
    os.environ['ROUTE_PLAN_DIRECTION_PROCESSES'] = '2' if args.mode == 'parallel' else '0'
    os.environ['ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS'] = str(args.comparison_seconds)
    WORK.mkdir(parents=True, exist_ok=True)
    snapshot_start = monotonic()
    cycle = '2026-11-04 00:00 UTC' if args.case == 'original' else '2026-11-08 00:00 UTC' if args.case in {'saved','hard'} else '2026-11-10 00:00 UTC'
    data = pinned_dataset(cycle,'100m AGL')
    prepare_seconds = monotonic()-snapshot_start
    if args.case == 'anomaly':
        connection = sqlite3.connect((BACKEND / 'routes.sqlite3').as_uri()+'?mode=ro', uri=True)
        row = connection.execute('SELECT route_id,points_json FROM routes WHERE route_id=?',('fadd1d52-4c28-4f3b-a2d2-0f683e17a786',)).fetchone()
        connection.close()
        assert row
        points = json.loads(row[1])
        departure = '2026-11-10T08:42:18.775Z'
        start, end = points[0][:2], points[-1][:2]
    elif args.case in {'saved','hard','original'}:
        examples = ROOT/'data/route_example_20261108_1600'
        body = json.loads((examples/('long-request.json' if args.case == 'hard' else 'north-request.json')).read_text(encoding='utf-8'))
        start,end,departure = body['start'],body['end'],body['departure_at']
        if args.case == 'saved':
            points = json.loads((examples/'verified-plan.json').read_text(encoding='utf-8'))['points']
        elif args.case == 'hard':
            points = json.loads((examples/'grid-candidates.json').read_text(encoding='utf-8'))[6]['points']
        else:
            start,end,departure = [117.17468,30.91165],[118.13049,30.39657],'2026-11-04T17:00:00+08:00'
            points = json.loads((ROOT/'data/cross_time_avoidance_tests/classic-user-case-full-lpa_star.json').read_text(encoding='utf-8'))['points']
    else:
        start, end = [119.56970,31.22689], [119.96521,30.55753]
        departure = '2026-11-10T08:37:04.845Z'
        points = None
        if args.case == 'short':
            end = [119.575,31.226]
    request = RoutePlanRequest(start=start,end=end,source='wrf',level='100m AGL',planner_type='wa_lpa_star',
        planning_strategy='wind_avoidance',departure_at=departure,average_ground_speed_mps=10,time_aware=True)
    profile_request = FlightProfileRequest(departure_at=request.departure_at,average_ground_speed_mps=10,
                                          thresholds=request.thresholds,wind_shear=request.wind_shear)
    report = dict(case=args.case,mode=args.mode,prepare_seconds=prepare_seconds,writes_platform_history=False,
                  budget=args.budget,benchmark_comparison_seconds=args.comparison_seconds)
    if args.case == 'first' and (WORK/'first-original-reference.json').exists():
        reference = json.loads((WORK/'first-original-reference.json').read_text(encoding='utf-8'))
        points = reference['points']
    if points:
        weather = LocalWeather(data, request, build_strategy_cost_config(request.planning_strategy))
        original = build_profile(dict(route_id='reference',name='reference',points=points,level=request.level,source='wrf'),profile_request,data)
        cleaned,cost,refinement = RouteRefiner(weather, points).finish()
        profile = build_profile(dict(route_id='cleaned',name='cleaned',points=cleaned,level=request.level,source='wrf'),profile_request,data)
        assert profile['decision']['status']=='allowed'
        # A surviving geometric loop must fail every full-voyage deletion check.
        assert all(not math.isfinite(weather.route_cost(trial)) for trial in cycle_proposals(cleaned))
        assert original['decision']['status'] == 'allowed'
        if args.case in {'anomaly','first'}:
            assert profile['analysis']['total_distance_km'] < original['analysis']['total_distance_km'] - 10
        report['saved_reference'] = dict(route_id=row[0] if args.case == 'anomaly' else f'{args.case}-reference',old_distance_km=original['analysis']['total_distance_km'],
            new_distance_km=profile['analysis']['total_distance_km'],old_cost=weather.route_cost(points),new_cost=cost,
            refinement=refinement,decision=profile['decision'])
        (WORK/f'{args.case}-cleaned-reference.json').write_text(json.dumps(dict(points=cleaned,profile=profile,refinement=refinement),ensure_ascii=False),encoding='utf-8')
    warmup = monotonic()
    if args.mode == 'parallel' and not args.cold_workers:
        warm_direction_pool()
    report['worker_warmup_seconds'] = monotonic()-warmup
    publications,progress = [],[]
    began = monotonic()
    def publish(result):
        assert result['analysis']['navigation_allowed']
        publications.append(dict(seconds=monotonic()-began,distance_km=result['distance_km'],cost=result['objective_cost'],shape=result['shape_quality']))
        (WORK/f'{args.case}-{args.mode}-plan.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        print(json.dumps({'publication':publications[-1]}),flush=True)
    status,result = 'completed',None
    try:
        with planning_scope(monotonic()+args.budget,Event(),progress=lambda p: progress.append(p)):
            result = compute_temporal_plan(request,data,[117,29.7,120.15,32.4],0,publish)
    except PlanningTimedOut:
        status='test_budget_expired'
    finally:
        shutdown_direction_pool()
    report.update(status=status,seconds=monotonic()-began,publications=publications,
                  diagnostics=result.get('planning_diagnostics') if result else progress[-1]['statistics'] if progress else None,
                  source_hashes=data.source_hashes)
    (WORK/f'{args.case}-{args.mode}-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key not in ['source_hashes','diagnostics']},ensure_ascii=False),flush=True)
    assert publications, 'Known real-WRF route must produce a validated solution'


if __name__ == '__main__':
    main()
