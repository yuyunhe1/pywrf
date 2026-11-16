"""GFS source isolation across search, continuous decisions, replay and saving."""
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import time
import hashlib

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import gfs_provider as gfs, route_storage, temporal_plan_service as service, wrf_timeline as timeline
from app.departure_windows import evaluate_saved_route
from app.flight_replay import build_profile
from app.models import DepartureWindowsRequest, FlightProfileRequest, RoutePlanRequest, RouteRecord
from app.route_context import geometry_route
from app.temporal_plan_service import compute_temporal_plan
from app.wind_provider import WindGrid

BASE = datetime(2026, 11, 4, tzinfo=timezone.utc)
START, END = (117., 30.), (117.6, 30.)


@pytest.fixture
def gfs_files(monkeypatch, tmp_path):
    records, grids = [], {}
    for hour in range(3):
        path = tmp_path / f'gfs.0p25.2026110400.f{hour:03d}.grib2'
        path.write_bytes(f'file-{hour}'.encode())
        record = gfs.describe_grib(path)
        records.append(record)
        grids[hour] = WindGrid(np.linspace(117, 117.6, 7), np.linspace(30.2, 29.8, 5),
                              np.full((5, 7), 2.), np.zeros((5, 7)), record.cycle, hour, '100m AGL',
                              record.valid_time, source='GFS')
    written = {}
    def discover():
        # A compressed synthetic GRIB decoder with real immutable byte inputs.
        # Mutations simulate newly published file revisions before discovery.
        for item in records:
            grid = grids[item.forecast_hour]
            signature = hashlib.sha256(grid.u.tobytes() + grid.v.tobytes()).hexdigest()
            if written.get(item.forecast_hour) != signature:
                with item.path.open('wb') as stream:
                    np.savez_compressed(stream, u=grid.u, v=grid.v, hour=item.forecast_hour)
                written[item.forecast_hour] = signature
        return tuple(records)
    def decode(item, level):
        with np.load(item.path, allow_pickle=False) as values:
            return replace(grids[item.forecast_hour], u=values['u'].copy(), v=values['v'].copy())
    monkeypatch.setenv('FORECAST_DERIVED_CACHE_DIR', str(tmp_path / 'derived'))
    monkeypatch.setattr(gfs, 'discover_files', discover)
    monkeypatch.setattr(gfs, 'get_frozen_grid', decode)
    monkeypatch.setattr(timeline, '_DATASETS', OrderedDict())
    return records, grids


def test_gfs_source_defaults_and_v3_restriction():
    request = RoutePlanRequest(start=START, end=END, source='gfs', level='100m AGL', time_aware=True)
    assert request.average_ground_speed_mps == 50
    assert DepartureWindowsRequest(source='gfs').average_ground_speed_mps == 50
    assert DepartureWindowsRequest(source='gfs', average_ground_speed_mps=35).average_ground_speed_mps == 35
    assert RoutePlanRequest(start=START, end=END, source='wrf', level='100m AGL', time_aware=True).average_ground_speed_mps == 10
    with pytest.raises(ValueError, match='原生 WRF'):
        RoutePlanRequest(start=START, end=END, source='gfs', level='100m AGL', planner_type='wrf_v3')


def test_missing_future_gfs_variable_is_unknown_in_windows_and_map_not_a_server_error(gfs_files, monkeypatch):
    from app.departure_windows import evaluate_saved_route
    original = gfs.get_frozen_grid
    def decoder(item, level):
        if item.forecast_hour == 1:
            raise ValueError('missing V wind variable')
        return original(item, level)
    monkeypatch.setattr(gfs, 'get_frozen_grid', decoder)
    data = timeline.load_gfs_dataset('100m AGL', BASE)
    route = geometry_route([START, END], '100m AGL', source='gfs')
    windows = evaluate_saved_route(route, DepartureWindowsRequest(source='gfs'), BASE, dataset=data)
    assert windows['current_departure']['status'] == 'unknown'
    frame = timeline.wind_frame(data, BASE.timestamp() + 1800)
    assert not frame['available'] and 'missing V' in frame['reason']


def test_gfs_arrays_are_pinned_to_one_cycle_and_file_revision(gfs_files):
    records, grids = gfs_files
    first = timeline.load_gfs_dataset('100m AGL', BASE)
    assert first.source == 'gfs' and len(first.frames) == 3
    assert timeline.load_gfs_dataset('100m AGL', BASE) is first
    records[1].path.write_bytes(b'a-new-file-revision')
    grids[1] = replace(grids[1], u=np.full((5, 7), 4.))
    second = timeline.load_gfs_dataset('100m AGL', BASE)
    assert second.dataset_id != first.dataset_id
    assert first.frames[1][1].u[0, 0] == 2
    assert second.frames[1][1].u[0, 0] == 4
    assert timeline.get_dataset(first.dataset_id) is first


def test_prepare_uses_the_selected_gfs_cycle_and_rejects_mixed_sources(gfs_files, monkeypatch):
    records, _ = gfs_files
    monkeypatch.setattr(gfs, 'find_file_by_valid_time', lambda at: records[0])
    request = RoutePlanRequest(start=START, end=END, source='gfs', level='100m AGL',
                               time_aware=True, valid_time=records[0].valid_time, departure_at=BASE)
    prepared, data = service.prepare_temporal_request(request)
    assert prepared.source == data.source == 'gfs'
    assert prepared.average_ground_speed_mps == 50 and data.cycle == records[0].cycle
    with pytest.raises(ValueError, match='数据源'):
        service.prepare_temporal_request(prepared.model_copy(update={'source': 'wrf'}))


@pytest.mark.parametrize('algorithm', ['astar', 'lpa_star', 'wa_lpa_star'])
def test_gfs_planners_avoid_hard_wind_and_keep_gfs_profiles(gfs_files, algorithm):
    _, grids = gfs_files
    for grid in grids.values():
        grid.u[1:4, 2:5] = 9
    data = timeline.load_gfs_dataset('100m AGL', BASE)
    request = RoutePlanRequest(start=START, end=END, source='gfs', level='100m AGL', time_aware=True,
                               departure_at=BASE, planner_type=algorithm, wind_shear={'enabled': False})
    route = compute_temporal_plan(request, data, [116.9, 29.7, 117.7, 30.3], 1000)
    assert route['source'] == route['route_context']['source'] == route['flight_profile']['source'] == 'gfs'
    assert route['analysis']['navigation_allowed']
    assert any(abs(point[1] - START[1]) > .1 for point in route['points'])
    assert route['average_ground_speed_mps'] == 50
    assert route['analysis']['max_wind_speed'] < request.thresholds.danger


def test_gfs_cross_hour_hazard_blocks_flight_and_future_windows(gfs_files):
    _, grids = gfs_files
    for hour in (1, 2):
        grids[hour].u[:] = 12
    data = timeline.load_gfs_dataset('100m AGL', BASE)
    route = geometry_route([START, END], '100m AGL', source='gfs')
    request = FlightProfileRequest(source='gfs', departure_at=BASE + timedelta(minutes=40), dataset_id=data.dataset_id)
    profile = build_profile(route, request, data)
    assert profile['source'] == 'gfs' and profile['decision']['status'] == 'blocked'
    windows = evaluate_saved_route(route, DepartureWindowsRequest(source='gfs'), BASE + timedelta(minutes=40), dataset=data)
    assert windows['source'] == 'gfs' and not windows['departure_windows']
    assert timeline.wind_frame(data, BASE.timestamp())['metadata']['source'].startswith('GFS')
    default_profile = build_profile(route, FlightProfileRequest(departure_at=BASE), data)
    default_windows = evaluate_saved_route(route, DepartureWindowsRequest(), BASE, dataset=data)
    assert default_profile['average_ground_speed_mps'] == default_windows['average_ground_speed_mps'] == 50
    gap = replace(data, frames=(data.frames[0], data.frames[2]))
    assert timeline.vector_at(gap, (BASE + timedelta(minutes=30)).timestamp(), *START) is None
    with pytest.raises(ValueError, match='数据源'):
        build_profile({**route, 'source': 'wrf'}, request, data)


def test_gfs_save_reload_preserves_source_and_legacy_routes_default_to_wrf(monkeypatch, tmp_path):
    monkeypatch.setattr(route_storage, 'DB_PATH', tmp_path / 'routes.sqlite3')
    payload = dict(name='GFS route', start=START, end=END, points=[START, END], level='100m AGL', source='gfs')
    record = route_storage.save_route(RouteRecord(**payload).model_dump())
    assert route_storage.get_route(record['route_id'])['source'] == 'gfs'
    payload.pop('source')
    assert route_storage.save_route(RouteRecord(**payload).model_dump())['source'] == 'wrf'


def test_gfs_job_windows_profile_and_replay_api_share_the_same_dataset(gfs_files, monkeypatch):
    from app import main, departure_windows
    monkeypatch.setattr(service, '_current_utc', lambda: BASE)
    monkeypatch.setattr(departure_windows, '_current_utc', lambda: BASE)
    monkeypatch.setattr(service, 'load_dataset', lambda *_: pytest.fail('GFS planning must not load WRF'))
    client = TestClient(main.app)
    response = client.post('/api/route/plan/jobs', json={'start': START, 'end': END, 'source': 'gfs',
        'level': '100m AGL', 'time_aware': True, 'departure_at': BASE.isoformat()})
    assert response.status_code == 202, response.text
    job = response.json()
    deadline = time.monotonic() + 5
    while job['status'] not in {'completed', 'failed', 'timed_out'}:
        assert time.monotonic() < deadline
        time.sleep(.01)
        job = client.get('/api/route/plan/jobs/' + job['job_id']).json()
    assert job['status'] == 'completed', job
    route = job['result']
    assert route['source'] == 'gfs' and route['average_ground_speed_mps'] == 50
    payload = {'points': route['points'], 'level': '100m AGL', 'source': 'gfs', 'dataset_id': route['dataset_id']}
    windows = client.post('/api/route/departure-windows', json=payload)
    assert windows.status_code == 200, windows.text
    assert windows.json()['source'] == 'gfs' and windows.json()['departure_windows']
    profile = client.post('/api/route/flight-profile', json={**payload, 'departure_at': BASE.isoformat()})
    assert profile.status_code == 200, profile.text
    assert profile.json()['dataset_id'] == route['dataset_id'] and profile.json()['source'] == 'gfs'
    frame = client.get('/api/flight-replay/wind', params={'dataset_id': route['dataset_id'], 'at': BASE.isoformat()})
    assert frame.status_code == 200 and frame.json()['metadata']['source'].startswith('GFS')
