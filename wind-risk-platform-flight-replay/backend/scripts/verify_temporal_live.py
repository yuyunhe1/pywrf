"""Verify the new planner with local real WRF files and a read-only saved route."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from threading import Event
from time import monotonic

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
sys.stdout.reconfigure(encoding='utf-8')
os.environ.update(WRF_CACHE_DIR=str(BACKEND.parent / 'data/wrf_platform_cache'), WRF_CACHE_AUTO_SYNC_INDEX='0',
                  WRF_CACHE_SYNC_ON_STARTUP='0', WRF_CACHE_REMOTE_HOST='', GFS_AUTO_DOWNLOAD='0')
from app.main import compute_route_plan
from app.models import RoutePlanRequest
from app.planning_control import PlanningTimedOut, planning_scope
from app.temporal_plan_service import prepare_temporal_request
from app.wrf_timeline import vector_at, iso

with sqlite3.connect((BACKEND / 'routes.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
    row = db.execute('SELECT name,start_json,end_json,level FROM routes ORDER BY updated_at DESC LIMIT 1').fetchone()
if row is None:
    raise RuntimeError('No saved route available for read-only real-data verification')
req = RoutePlanRequest(source='wrf', time_aware=True, start=json.loads(row[1])[:2], end=json.loads(row[2])[:2],
                        level=row[3], average_ground_speed_mps=10)
started = monotonic()
req, data = prepare_temporal_request(req)
preparation = monotonic() - started
publications = []
def publish(candidate):
    assert candidate['analysis']['navigation_allowed']
    publications.append((monotonic() - started, candidate))
try:
    with planning_scope(monotonic() + 40, Event()):
        result = compute_route_plan(req, publish=publish, dataset=data)
except PlanningTimedOut:
    if not publications:
        raise
    result = publications[-1][1]
profile = result['flight_profile']
assert result['dataset_id'] == profile['dataset_id'] == result['departure_windows']['dataset_id']
assert data.level == profile['level'] == req.level
assert profile['analysis'] == result['analysis']
matched = 0
for sample in profile['analysis']['samples'][::max(1, len(profile['analysis']['samples']) // 40)]:
    stamp = datetime.fromisoformat(sample['eta'].replace('Z', '+00:00')).timestamp()
    vector = vector_at(data, stamp, sample['lon'], sample['lat'])
    if vector is None:
        assert sample['wind_speed'] is None
    else:
        assert math.isclose(sample['wind_speed'], math.hypot(*vector), abs_tol=.00001)
    expected = req.departure_at.timestamp() + sample['distance_km'] * 1000 / req.average_ground_speed_mps
    assert abs(stamp - expected) < .0011
    matched += 1
report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'route_name': row[0], 'forecast_cycle': data.cycle,
          'dataset_id': data.dataset_id, 'frames': len(data.frames), 'level': data.level,
          'coverage_start': iso(data.coverage_start), 'coverage_end': iso(data.coverage_end),
          'departure_at': profile['departure_at'], 'arrival_at': profile['arrival_at'], 'decision': profile['decision'],
          'distance_km': result['distance_km'], 'preparation_seconds': preparation, 'elapsed_seconds': monotonic() - started,
          'first_feasible_seconds': publications[0][0] if publications else None, 'matched_eta_wind_samples': matched,
          'future_windows': result['departure_windows']['departure_windows'], 'reference_route': result['reference_route'],
          'planning_message': result.get('planning_message')}
target = ROOT / 'data/temporal_route_tests'
target.mkdir(parents=True, exist_ok=True)
(target / 'live-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
(target / 'live-plan.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
