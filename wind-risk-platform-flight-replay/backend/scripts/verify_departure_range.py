"""Isolated real-WRF joint-range benchmark; no production clock/history writes."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.update(WRF_NATIVE_10MIN_REQUIRED='1', GFS_AUTO_DOWNLOAD='0', WRF_CACHE_AUTO_SYNC_INDEX='0',
    WRF_CACHE_REMOTE_HOST='', WIND_FRAME_WARMUP='0', ROUTE_PLAN_DIRECTION_PROCESSES='2',
    ROUTE_PLAN_WEATHER_LOADING='full', ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='0',
    ROUTE_PLAN_MAX_TEMPORAL_LABELS='600000', ROUTE_PLAN_MEMORY_RESERVE_MB='2048',
    ROUTE_PLAN_PROCESS_MEMORY_MB='2048', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
from app.departure_range import DepartureRangeWorkflow
from app.direction_pool import shutdown_direction_pool
from app.models import RecommendationJobRequest
from app.planning_control import planning_scope, PlanningTimedOut
from app.planning_limits import process_peak_memory_mb
from app.route_quality import assess_route_quality
from app.wrf_timeline import load_dataset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=90)
    parser.add_argument('--attempt-seconds', type=float, default=60)
    args = parser.parse_args()
    os.environ['ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS'] = str(args.attempt_seconds)
    fixture = json.loads((BACKEND/'tests/fixtures/route_quality_user_20261115.json').read_text(encoding='utf-8'))
    start = datetime.fromisoformat(fixture['departure_at'].replace('Z', '+00:00'))
    end = start.replace(hour=12, minute=40)
    request = RecommendationJobRequest(mode='departure_range', required_waypoints=[fixture['points'][0],fixture['points'][-1]],
        departure_start_at=start, departure_end_at=end, cycle=fixture['cycle'], source='wrf', level=fixture['level'],
        average_ground_speed_mps=10, planning_strategy='wind_avoidance', planner_type='wa_lpa_star',
        prefer_simple_route=True)
    began = monotonic()
    data = load_dataset(request.level, start, request.cycle)
    prepared = monotonic()-began
    state, publications = {}, []
    def report(value): state.update(value)
    def publish(value):
        row = {key: value.get(key) for key in ('departure_at','arrival_at','distance_km','max_contact_wind_speed_mps',
            'min_verified_peak_mps','peak_wind_limit_mps','selection_revision','range_comparison_complete')}
        row.update(elapsed_seconds=monotonic()-began,
            windows=value.get('departure_windows'), quality=assess_route_quality(value['points'],10))
        publications.append(row)
        print(json.dumps({key: row[key] for key in ('elapsed_seconds','departure_at','distance_km','max_contact_wind_speed_mps')},ensure_ascii=True),flush=True)
    work, status = None, 'completed'
    try:
        with planning_scope(monotonic()+args.seconds,Event()):
            work = DepartureRangeWorkflow(request,data,report,publish,now=lambda:start)
            work.run()
    except PlanningTimedOut:
        status = 'isolated_budget_partial'
    finally:
        shutdown_direction_pool()
    result = dict(status=status, elapsed_seconds=monotonic()-began, preparation_seconds=prepared,
        configured_budget_seconds=args.seconds, attempt_budget_seconds=args.attempt_seconds,
        dataset_id=data.dataset_id, cycle=data.cycle, frame_count=len(data.frames), time_model=data.time_model,
        request=request.model_dump(mode='json'), publications=publications, workflow=state,
        process_peak_memory_mb=process_peak_memory_mb(), writes_history=False, production_clock_modified=False,
        best=work.best if work else None)
    target = ROOT/'data/departure_range_tests'
    target.mkdir(parents=True,exist_ok=True)
    (target/'real-wrf-range.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps(dict(status=status,publications=len(publications),first_seconds=publications[0]['elapsed_seconds'] if publications else None,
        memory_mb=result['process_peak_memory_mb'],evidence=str(target/'real-wrf-range.json')),ensure_ascii=True))
    if not work or not work.best:
        raise SystemExit('No verified candidate within the isolated benchmark budget')


if __name__ == '__main__': main()
