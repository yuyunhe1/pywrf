"""Read-only real weather checks for the first-verified-solution page mode."""
import json
import os
from pathlib import Path
import sys
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
    ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='0', WRF_NATIVE_10MIN_REQUIRED='1')
from app import wrf_cache_provider, gfs_provider
from app.models import RoutePlanRequest, FlightProfileRequest
from app.planning_control import planning_scope
from app.temporal_plan_service import compute_temporal_plan
from app.wrf_timeline import load_dataset, load_gfs_dataset
from app.flight_replay import build_profile
from app.direction_pool import shutdown_direction_pool, direction_pool


def main():
    work = ROOT / 'data/first_feasible_tests'
    work.mkdir(parents=True, exist_ok=True)
    endpoints = json.loads((ROOT / 'data/strategy_saved_tests/saved-cases.json').read_text(encoding='utf-8'))[0]
    departure = (datetime.now(timezone.utc) + timedelta(minutes=2)).replace(second=0, microsecond=0)
    rows = []
    try:
        for source in ('wrf', 'gfs'):
            cycle = (max(row['cycle'] for row in wrf_cache_provider.load_index()['files']) if source == 'wrf'
                else max(row.cycle for row in gfs_provider.discover_files()))
            before = monotonic()
            data = (load_dataset('100m AGL', departure, cycle) if source == 'wrf'
                else load_gfs_dataset('100m AGL', departure, cycle))
            preparation = monotonic() - before
            for algorithm in ('astar', 'lpa_star', 'wa_lpa_star'):
                req = RoutePlanRequest(source=source, start=endpoints['start'], end=endpoints['end'], level='100m AGL',
                    cycle=cycle, departure_at=departure, average_ground_speed_mps=10 if source == 'wrf' else 50,
                    time_aware=True, planner_type=algorithm, planning_strategy='wind_avoidance', stop_on_first_feasible=True)
                accepted, progress = [], []
                began = monotonic()
                with planning_scope(began + 60, Event(), progress=progress.append):
                    result = compute_temporal_plan(req, data, [-180, -90, 180, 90], 0,
                        lambda value: accepted.append(dict(seconds=monotonic()-began, distance_km=value['distance_km'])))
                elapsed = monotonic() - began
                assert result['analysis']['navigation_allowed'] and len(accepted) == 1, result.get('planning_message')
                attempt = result['planning_diagnostics']['attempts'][0]
                assert attempt['stop_reason'] == 'first_feasible' and attempt['complete_candidates'] == 1
                assert direction_pool().cancel.is_set()
                checked = build_profile(result['route_context'], FlightProfileRequest(departure_at=departure,
                    source=source, average_ground_speed_mps=req.average_ground_speed_mps, thresholds=req.thresholds,
                    wind_shear=req.wind_shear, dataset_id=data.dataset_id), data)
                assert checked['analysis']['navigation_allowed']
                row = dict(source=source, algorithm=algorithm, cycle=cycle, departure_at=departure.isoformat(),
                    writes_history=False, historical_departure_replayed=False, dataset_prepare_seconds=preparation,
                    elapsed_seconds=elapsed, candidates=accepted, distance_km=result['distance_km'],
                    max_wind=result['analysis']['max_wind_speed'], mean_wind=result['analysis']['mean_wind_speed'],
                    stop_reason=attempt['stop_reason'], worker_release_seconds=attempt['worker_release_seconds'],
                    progress_updates=len(progress), forward_expanded=attempt['forward_expanded'],
                    backward_expanded=attempt['backward_expanded'], result=result)
                rows.append(row)
                (work / f'real-{source}-{algorithm}.json').write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({k:v for k,v in row.items() if k not in ('result','candidates')}, ensure_ascii=True), flush=True)
        (work / 'real-summary.json').write_text(json.dumps([{k:v for k,v in row.items() if k != 'result'} for row in rows],
            ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        shutdown_direction_pool()


if __name__ == '__main__':
    main()
