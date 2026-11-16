"""Long-route acceptance: temporal bypass, first publication, same-model replay."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from threading import Event
from time import monotonic

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models import RoutePlanRequest
from app.planning_control import planning_scope
from app.temporal_plan_service import compute_temporal_plan
from app.wrf_timeline import ForecastDataset
from app.wind_provider import WindGrid

base = datetime(2026, 11, 3, tzinfo=timezone.utc)
frames = []
for hour in range(10):
    lons, lats = np.linspace(118.4, 119.6, 61), np.linspace(31.4, 31.0, 21)
    u = np.full((len(lats), len(lons)), 6.)
    if hour >= 1:
        u[8:13, 27:34] = 12
    grid = WindGrid(lons, lats, u, np.zeros_like(u), base.strftime('%Y-%m-%d %H:%M UTC'), hour, '100m AGL',
                    (base + timedelta(hours=hour)).strftime('%Y-%m-%d %H:%M UTC'), source='WRF')
    frames.append(((base + timedelta(hours=hour)).timestamp(), grid))
data = ForecastDataset('long-route-fixture', frames[0][1].cycle, '100m AGL', tuple(frames), frames[0][0], frames[-1][0])
report = []
for algorithm in ('astar', 'lpa_star', 'wa_lpa_star'):
    req = RoutePlanRequest(time_aware=True, source='wrf', start=(118.45, 31.2), end=(119.55, 31.2),
                            level='100m AGL', departure_at=base, planner_type=algorithm,
                            wind_shear={'enabled': False})
    began, publications = monotonic(), []
    def publish(value):
        assert value['analysis']['navigation_allowed'] is True
        publications.append({'elapsed': monotonic() - began, 'distance_km': value['distance_km']})
    with planning_scope(monotonic() + 60, Event()):
        result = compute_temporal_plan(req, data, [118.4, 31.0, 119.6, 31.4], 20000, publish)
    assert result['analysis']['navigation_allowed']
    assert any(abs(point[1] - 31.2) > .04 for point in result['points'])
    assert publications and publications[0]['elapsed'] < 30
    assert result['flight_profile']['dataset_id'] == result['departure_windows']['dataset_id']
    report.append({'algorithm': algorithm, 'elapsed_seconds': monotonic() - began, 'publications': publications,
                   'distance_km': result['distance_km'], 'duration_hours': result['flight_profile']['flight_duration_seconds'] / 3600,
                   'planning_message': result.get('planning_message'), 'returned_planner_type': result['planner_type']})
target = Path(__file__).resolve().parents[3] / 'data/temporal_route_tests'
target.mkdir(parents=True, exist_ok=True)
(target / 'long-route-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
