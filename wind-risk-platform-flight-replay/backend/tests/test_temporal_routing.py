from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main, departure_windows, temporal_plan_service as service, wrf_timeline
from app.departure_windows import prepare_evaluation
from app.flight_replay import build_profile
from app.models import FlightProfileRequest, RoutePlanRequest
from app.temporal_routing import TemporalPlanner, TemporalWeather
from app.route_options import build_strategy_cost_config
from app.wind_provider import WindGrid

BASE = datetime(2026, 11, 3, tzinfo=timezone.utc)
START, END = (0., 0.), (.6, 0.)


def dataset(transform=None, hours=range(6)):
    frames = []
    for hour in hours:
        u, v = np.ones((5, 7)), np.zeros((5, 7))
        if transform:
            transform(hour, u, v)
        grid = WindGrid(np.linspace(0, .6, 7), np.linspace(.2, -.2, 5), u, v,
                        BASE.strftime('%Y-%m-%d %H:%M UTC'), hour, '100m AGL',
                        (BASE + timedelta(hours=hour)).strftime('%Y-%m-%d %H:%M UTC'), source='WRF')
        frames.append(((BASE + timedelta(hours=hour)).timestamp(), grid))
    return wrf_timeline.ForecastDataset('temporal-test', frames[0][1].cycle, '100m AGL', tuple(frames), frames[0][0], frames[-1][0])


def request(**kwargs):
    return RoutePlanRequest(start=START, end=END, source='wrf', level='100m AGL', time_aware=True,
                            departure_at=BASE, wind_shear={'enabled': False}, **kwargs)


def planner(data, req=None, **kwargs):
    req = req or request()
    weather = TemporalWeather(data, req, build_strategy_cost_config('distance_priority'))
    return TemporalPlanner(weather, req.start, req.end, [-.1, -.3, .7, .3], **kwargs)


def profile(data, result, req=None):
    req = req or request()
    return build_profile({'route_id': 'test', 'name': 'test', 'points': result['points'], 'level': req.level},
                         FlightProfileRequest(departure_at=req.departure_at, average_ground_speed_mps=req.average_ground_speed_mps,
                                              wind_shear=req.wind_shear), data)


@pytest.mark.parametrize('algorithm', ['astar', 'lpa_star', 'wa_lpa_star'])
def test_search_detours_around_future_hazard_that_is_safe_in_departure_snapshot(algorithm):
    def wind(hour, u, v):
        if hour >= 1:
            u[2, 3] = 12
    data = dataset(wind)
    result = planner(data).search(algorithm)
    assert any(abs(point[1]) >= .09 for point in result['points'])
    assert profile(data, result)['decision']['status'] == 'allowed'
    assert profile(data, {'points': [START, END]})['decision']['status'] == 'blocked'
    assert result['arrival_labels'] > 1
    assert result['distance_km'] * 100 == pytest.approx(profile(data, result)['flight_duration_seconds'])


@pytest.mark.parametrize('algorithm', ['astar', 'lpa_star', 'wa_lpa_star'])
def test_destination_is_checked_at_arrival_instead_of_departure(algorithm):
    def wind(hour, u, v):
        if hour == 0:
            u[:, -1] = 12
    data = dataset(wind)
    result = planner(data).search(algorithm)
    assert profile(data, result)['decision']['status'] == 'allowed'
    assert profile(data, result)['analysis']['samples'][-1]['wind_speed'] == 1


@pytest.mark.parametrize('algorithm', ['astar', 'lpa_star', 'wa_lpa_star'])
def test_later_arrival_label_at_same_cell_survives_cheaper_early_label(algorithm):
    def wind(hour, u, v):
        u[:, 4] = 10 if hour < 2 else 1
    data = dataset(wind)
    req = request().model_copy(update={'end': (.4, 0.)})
    search = planner(data, req)
    s, a, b, join, goal = (2, 0), (2, 1), (1, 1), (2, 2), (2, 4)
    graph = {s: [a, b], a: [join], b: [join], join: [goal], goal: []}
    search.start_id, search.goal_id = s, goal
    search.neighbours = lambda node: graph[node]
    result = search.search(algorithm)
    assert any(point[1] > .09 for point in result['points'])
    assert profile(data, result, req)['decision']['status'] == 'allowed'
    assert profile(data, {'points': [req.start, req.end]}, req)['decision']['status'] == 'blocked'


def test_continuous_edge_constraints_match_fixed_route_evaluator_including_shear():
    def wind(hour, u, v):
        u[:] = 2 if hour != 1 else 7.900001
        u[1, 3] = -u[1, 3]
        v[3, 3] = np.nan if hour == 2 else 0
    data = dataset(wind)
    req = request().model_copy(update={'wind_shear': FlightProfileRequest(departure_at=BASE).wind_shear})
    weather = planner(data, req).weather
    for a, b in [(START, END), ((.1, .1), (.4, .1)), ((.1, -.1), (.5, -.1))]:
        edge = weather.edge(a, b)
        evaluation = prepare_evaluation([a, b], list(data.frames), 10, 7.9, 5.4)
        for offset in [0, 400, 1000, 1750, 3550, 3600, 5000, 7199, 12000, 18000]:
            stamp = BASE.timestamp() + offset
            assert edge.allowed(stamp) == (not any(issue.departure.contains(stamp) for issue in evaluation.issues))


def test_missing_hour_and_forecast_end_are_not_extrapolated():
    for data in [dataset(hours=[0, 2, 3]), dataset(hours=[0, 1])]:
        with pytest.raises(ValueError, match='未找到'):
            planner(data).search()


def test_thinned_edges_still_check_original_cells_between_search_nodes():
    def wind(hour, u, v):
        u[:, 3] = 12
    data = dataset(wind)
    with pytest.raises(ValueError, match='未找到'):
        planner(data, max_grid_cells=4).search()


@pytest.mark.parametrize('speed', [0, -1, float('nan'), float('inf')])
def test_invalid_speed_is_rejected(speed):
    with pytest.raises(ValueError):
        request(average_ground_speed_mps=speed)


def test_unzoned_departure_and_unknown_source_are_rejected():
    payload = request().model_dump()
    payload['departure_at'] = BASE.replace(tzinfo=None)
    with pytest.raises(ValueError, match='时区'):
        RoutePlanRequest(**payload)
    payload.update(departure_at=BASE, source='unknown')
    with pytest.raises(ValueError, match='WRF'):
        RoutePlanRequest(**payload)


def test_decimal_anchors_do_not_create_zero_duration_profile_segments():
    from app.flight_replay import RouteGeometry
    geometry = RouteGeometry([(118.45, 31.1), (119.4, 31.099999999999998), (119.4, 31.1)], 10)
    assert all(np.isfinite(geometry.at(value)).all() for value in [0, geometry.duration / 2, geometry.duration])


def test_a_blocked_start_returns_no_route_or_fabricated_reference(monkeypatch):
    def wind(hour, u, v):
        if hour == 0:
            u[:] = 10
    data = dataset(wind)
    monkeypatch.setattr(departure_windows, '_current_utc', lambda: BASE)
    published = []
    result = service.compute_temporal_plan(request(), data, [-.1, -.3, .7, .3], 20000, published.append)
    assert not published
    assert not result['reference_route'] and result['points'] == []
    assert not result['analysis']['navigation_allowed']
    assert 'flight_profile' not in result and 'route_context' not in result
    assert result['route_status'] == 'no_feasible_route'


@pytest.mark.parametrize('algorithm', ['astar', 'lpa_star', 'wa_lpa_star'])
def test_soft_wind_corridor_is_first_publication_and_windows_do_not_block_it(algorithm, monkeypatch):
    def wind(hour, u, v):
        u[2, 2:5] = 6  # flyable by hard limit; still worth avoiding
    data = dataset(wind)
    req = request(planner_type=algorithm, planning_strategy='wind_avoidance')
    monkeypatch.setattr(departure_windows, 'evaluate_saved_route', lambda *a, **k: pytest.fail('windows must not block first route'))
    published = []
    result = service.compute_temporal_plan(req, data, [-.1, -.3, .7, .3], 20000, published.append)
    # A checked bridge may clear the closed cell boundary without visiting
    # its adjacent grid centre; judge actual exposure, not a particular turn.
    assert published and any(abs(p[1]) > .05 for p in result['points'])
    assert profile(data, result, req)['analysis']['max_wind_speed'] < 2
    assert all(r['analysis']['navigation_allowed'] and r['planner_type'] != 'direct_preview' for r in published)
    weather = TemporalWeather(data, req, build_strategy_cost_config('wind_avoidance'))
    assert result['objective_cost'] < weather.route_cost([START, END])
    assert [r['objective_cost'] for r in published] == sorted([r['objective_cost'] for r in published], reverse=True)


def test_snapshot_candidate_unsafe_in_future_is_repaired_before_publication():
    def wind(hour, u, v):
        if hour >= 1:
            u[2, 3] = 12
    data = dataset(wind)
    published = []
    result = service.compute_temporal_plan(request(planning_strategy='wind_avoidance'), data, [-.1, -.3, .7, .3], 20000, published.append)
    assert published and all(profile(data, r)['decision']['status'] == 'allowed' for r in published)
    # A checked bridge may turn between grid centres; clearing the cell's
    # half-width is enough and the independent profile proves its safety.
    assert any(abs(p[1]) > .05 for p in result['points'])
    assert profile(data, {'points': [START, END]})['decision']['status'] == 'blocked'


def test_goal_unsafe_at_takeoff_does_not_reject_future_arrival_in_corridor_service():
    def wind(hour, u, v):
        if hour == 0:
            u[2, -1] = 12
    data = dataset(wind)
    result = service.compute_temporal_plan(request(), data, [-.1, -.3, .7, .3], 20000)
    assert result['route_status'] == 'feasible'
    assert result['analysis']['navigation_allowed']


def test_uniform_field_returns_a_straight_route_only_after_spatial_search():
    result = service.compute_temporal_plan(request(), dataset(), [-.1, -.3, .7, .3], 20000)
    assert result['points'] == [START, END]
    assert result['search_phase'] == 'searched_shortcut'
    attempt = result['planning_diagnostics']['attempts'][0]
    assert attempt['phase'] == 'segmented_bidirectional_search'
    assert attempt['forward_expanded'] > 0 and attempt['backward_expanded'] > 0


def test_missing_forecast_returns_no_geometry_or_fabricated_flight():
    data = dataset(hours=[1, 2])
    result = service.compute_temporal_plan(request(), data, [-.1, -.3, .7, .3], 20000)
    assert result['route_status'] == 'data_insufficient' and not result['points']
    assert 'flight_profile' not in result


def test_missing_hour_inside_the_shortest_flight_is_data_insufficient():
    result = service.compute_temporal_plan(request(), dataset(hours=[0, 2, 3]), [-.1, -.3, .7, .3], 20000)
    assert result['route_status'] == 'data_insufficient' and not result['points']


def test_resource_limit_is_not_reported_as_a_successful_straight_reference(monkeypatch):
    monkeypatch.setenv('ROUTE_PLAN_MAX_TEMPORAL_LABELS', '1000')
    def wind(hour, u, v):
        u[:, 3] = 12
    result = service.compute_temporal_plan(request(), dataset(wind), [-.1, -.3, .7, .3], 20000)
    assert result['route_status'] == 'resource_limit' and not result['points']
    assert not result['reference_route'] and 'route_context' not in result
    assert any(a.get('arrival_labels', 0) >= 1000 for a in result['planning_diagnostics']['attempts'])


def test_unsaved_plan_profile_windows_map_and_legacy_saved_route_share_weather(monkeypatch):
    data = dataset()
    monkeypatch.setattr(wrf_timeline, '_DATASETS', OrderedDict([(data.dataset_id, data)]))
    monkeypatch.setattr(service, 'load_dataset', lambda level, now: data)
    monkeypatch.setattr(service, '_current_utc', lambda: BASE)
    monkeypatch.setattr(departure_windows, '_current_utc', lambda: BASE)
    monkeypatch.setattr(departure_windows, 'load_dataset', lambda level, now: data)
    monkeypatch.setattr(main.route_storage, 'save_route', lambda *a, **k: pytest.fail('analysis must not persist a route'))
    client = TestClient(main.app)
    result = client.post('/api/route/plan', json=request().model_dump(mode='json'))
    assert result.status_code == 200, result.text
    plan = result.json()
    assert plan['time_aware'] and not plan['reference_route']
    assert plan['dataset_id'] == plan['flight_profile']['dataset_id']
    assert plan['departure_windows'] is None  # windows cannot block publication
    assert plan['flight_profile']['decision']['status'] == 'allowed'
    assert len(plan['points'][0]) == 5  # retain export altitude information
    payload = {'points': plan['points'], 'level': '100m AGL', 'dataset_id': data.dataset_id, 'wind_shear': {'enabled': False}}
    windows = client.post('/api/route/departure-windows', json=payload)
    flight = client.post('/api/route/flight-profile', json={**payload, 'departure_at': BASE.isoformat()})
    assert windows.status_code == flight.status_code == 200
    assert windows.json()['route_id'] == flight.json()['route_id'] == plan['route_context']['route_id']
    assert flight.json()['analysis'] == plan['flight_profile']['analysis']
    monkeypatch.setattr(main.route_storage, 'get_route', lambda _: {**plan['route_context'], 'route_id': 'saved'})
    saved = client.post('/api/routes/saved/departure-windows', json={'wind_shear': {'enabled': False}}).json()
    assert saved['departure_windows'] == windows.json()['departure_windows']
    assert saved['current_departure'] == windows.json()['current_departure']
    for sample in flight.json()['analysis']['samples'][::60]:
        frame = client.get('/api/flight-replay/wind', params={'dataset_id': data.dataset_id, 'at': sample['eta']}).json()
        assert frame['available']
        assert sample['wind_speed'] == 1
    json.dumps(plan, allow_nan=False)


def test_job_freezes_now_and_dataset_before_queueing(monkeypatch):
    data, captured = dataset(), {}
    monkeypatch.setattr(service, 'load_dataset', lambda level, now: data)
    monkeypatch.setattr(service, '_current_utc', lambda: BASE)
    def submit(compute, req, **kwargs):
        captured.update(compute=compute, request=req)
        return {'job_id': 'queued'}
    monkeypatch.setattr(main.route_jobs, 'submit', submit)
    req = request().model_copy(update={'departure_at': None})
    assert main.create_route_plan_job(req)['job_id'] == 'queued'
    monkeypatch.setattr(service, '_current_utc', lambda: BASE + timedelta(minutes=10))
    assert captured['request'].departure_at == BASE
    assert captured['request'].dataset_id == data.dataset_id
    published = []
    result = captured['compute'](captured['request'], publish=published.append)
    assert published and all(item['flight_profile']['decision']['status'] == 'allowed' for item in published)
    assert result['departure_at'].startswith('2026-11-03T00:00:00')
