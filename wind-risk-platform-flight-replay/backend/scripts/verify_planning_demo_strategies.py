"""Read frozen saved endpoints; compare strategies on a real native batch.

The old saved departure is outside current native coverage. These are endpoint
regressions at a new, explicitly recorded departure, not historical replays.
"""
import json
import argparse
import os
from pathlib import Path
import sys
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from threading import Event
from time import monotonic

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.update(GFS_AUTO_DOWNLOAD='0', WRF_CACHE_AUTO_SYNC_INDEX='0', WRF_CACHE_REMOTE_HOST='',
    WRF_V3_AUTO_SYNC='0', WIND_FRAME_WARMUP='0', ROUTE_PLAN_DIRECTION_PROCESSES='2',
    ROUTE_PLAN_MAX_TEMPORAL_LABELS='600000', ROUTE_PLAN_MEMORY_RESERVE_MB='2048',
    ROUTE_PLAN_PROCESS_MEMORY_MB='2048', ROUTE_PLAN_WEATHER_LOADING='full',
    ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='0')
from app import wrf_cache_provider, gfs_provider
from app.models import RoutePlanRequest
from app.planning_control import planning_scope, PlanningTimedOut
from app.temporal_plan_service import compute_temporal_plan
from app.wrf_timeline import load_dataset, load_gfs_dataset
from app.direction_pool import shutdown_direction_pool

WORK = ROOT / 'data/planning_demo_tests'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', choices=('wrf', 'gfs'), default='wrf')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--output-dir', type=Path, default=WORK)
    parser.add_argument('--departure-at')
    parser.add_argument('--cycle')
    parser.add_argument('--require-feasible', action='store_true')
    args = parser.parse_args()
    work = args.output_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    rows = json.loads((ROOT / 'data/strategy_saved_tests/saved-cases.json').read_text(encoding='utf-8'))
    cycle = args.cycle or (max(row['cycle'] for row in wrf_cache_provider.load_index()['files']) if args.source == 'wrf'
             else max(row.cycle for row in gfs_provider.discover_files()))
    now = datetime.now(timezone.utc)
    departure = (datetime.fromisoformat(args.departure_at) if args.departure_at else
        (now + timedelta(minutes=2)).replace(second=0, microsecond=0))
    data = (load_dataset('100m AGL', departure, cycle) if args.source == 'wrf'
            else load_gfs_dataset('100m AGL', departure, cycle))
    assert data.time_model == ('NATIVE_WRF_TEN_MINUTE_HOLD_V1' if args.source == 'wrf' else 'HOURLY_UV_TEN_MINUTE_HOLD_V1')
    speed = 10 if args.source == 'wrf' else 50
    prefix = 'real-' if args.source == 'wrf' else 'real-gfs-'
    if args.source == 'gfs': rows = rows[:1]
    (work / f'{args.source}-strategy-context.json').write_text(json.dumps(dict(cycle=cycle, departure_at=departure.isoformat(),
        source=args.source, level='100m AGL', dataset_id=data.dataset_id, historical_departure_replayed=False)), encoding='utf-8')
    try:
        for row in rows:
            for strategy in ('distance_priority', 'balanced', 'wind_avoidance'):
                request = RoutePlanRequest(source=args.source, start=row['start'], end=row['end'], level='100m AGL',
                    cycle=cycle, departure_at=departure, average_ground_speed_mps=speed,
                    time_aware=True, planner_type='wa_lpa_star', planning_strategy=strategy)
                best, candidates = None, []
                began = monotonic()
                def publish(value):
                    nonlocal best
                    best = deepcopy(value)
                    candidates.append(dict(seconds=monotonic()-began, distance_km=value['distance_km'],
                        mean_wind=value['analysis']['mean_wind_speed'], max_wind=value['analysis']['max_wind_speed'],
                        objective_cost=value['objective_cost']))
                status = 'completed'
                try:
                    with planning_scope(began + args.seconds, Event()):
                        result = compute_temporal_plan(request, data, [-180, -90, 180, 90], 0, publish)
                        if result.get('analysis', {}).get('navigation_allowed'): best = result
                        else: status = result.get('route_status', 'unconfirmed')
                except PlanningTimedOut:
                    status = 'budget_reached'
                except RuntimeError as error:
                    if str(error) != 'PlanningTimedOut': raise
                    status = 'budget_reached'
                report = dict(route_id=row['route_id'], strategy=strategy, cycle=cycle, departure_at=departure.isoformat(),
                    elapsed_seconds=monotonic()-began, status=status, candidates=candidates, writes_history=False,
                    distance_km=best['distance_km'] if best else None,
                    mean_wind=best['analysis']['mean_wind_speed'] if best else None,
                    max_wind=best['analysis']['max_wind_speed'] if best else None, result=best)
                (work / f"{prefix}{row['route_id'][:8]}-{strategy}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({k:v for k,v in report.items() if k not in ('result','candidates')},ensure_ascii=True), flush=True)
                if args.require_feasible:
                    assert best and best['analysis']['navigation_allowed'], f'{args.source} {strategy}: no verified result'
    finally:
        shutdown_direction_pool()


if __name__ == '__main__':
    main()
