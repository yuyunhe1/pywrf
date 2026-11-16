from threading import Event
from time import monotonic

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.route_jobs import RouteJobManager, PlanningQueueFull, candidate_score
from app.planning_control import checkpoint, planning_scope, stage_budget, PlanningCancelled, PlanningTimedOut, PlanningStageExpired


def test_search_progress_revisions_are_throttled_and_independent_from_results_and_windows():
    from app.planning_control import search_progress
    ready, resume, finalized = Event(), Event(), []
    manager = RouteJobManager(workers=1)
    def compute(_, publish):
        search_progress({'forward': [{'points': [[0, 0], [1, 0]]}], 'backward': []})
        search_progress({'forward': []})  # less than one second: retain first
        ready.set()
        assert resume.wait(3)
        return candidate()
    job = manager.submit(compute, None, on_finish=lambda result: finalized.append(result))
    try:
        assert ready.wait(3)
        state = manager.snapshot(job['job_id'])
        assert state['revision'] == 0 and state['result'] is None and not finalized
        assert state['progress_revision'] == 1
        assert state['search_progress']['forward'][0]['points']
        assert manager.snapshot(job['job_id'], 0, 1)['search_progress'] is None
        resume.set()
        manager.jobs[job['job_id']].future.result(3)
        done = manager.snapshot(job['job_id'], since_progress_revision=1)
        assert done['status'] == 'completed' and done['search_progress'] is None
        assert done['progress_revision'] == 2 and len(finalized) == 1
    finally:
        resume.set()
        manager.shutdown()


def candidate(distance=100, wind=3, allowed=True):
    return {"points": [[118, 31], [119, 32]], "distance_km": distance, "planning_strategy": "wind_avoidance",
            "analysis": {"navigation_allowed": allowed, "danger_ratio": 0., "max_wind_speed": wind, "mean_wind_speed": wind}}


def test_integrated_strategy_objective_overrides_inconsistent_peak_wind_ranking():
    low_total = {**candidate(100, 4), 'objective_cost': 100}
    low_peak = {**candidate(300, 2), 'objective_cost': 200}
    assert candidate_score(low_total) < candidate_score(low_peak)
    assert candidate_score({**low_total, 'reference_route': True}) > candidate_score(low_peak)


def test_first_route_visible_before_optimization_and_only_improvements_replace_it():
    ready, resume = Event(), Event()
    manager = RouteJobManager(workers=1)

    def compute(_, publish):
        publish(candidate())
        ready.set()
        assert resume.wait(3)
        publish(candidate(90))
        publish(candidate(80, wind=5))  # Shorter but worse for wind avoidance.
        return candidate(95)

    job = manager.submit(compute, None)
    try:
        assert ready.wait(3)
        first = manager.snapshot(job['job_id'])
        assert first['status'] == 'running'
        assert first['result']['distance_km'] == 100
        assert manager.snapshot(job['job_id'], first['revision'])['result'] is None
        resume.set()
        manager.jobs[job['job_id']].future.result(3)
        final = manager.snapshot(job['job_id'])
        assert final['status'] == 'completed'
        assert final['revision'] == 2
        assert final['result']['distance_km'] == 90
    finally:
        resume.set()
        manager.shutdown()


@pytest.mark.parametrize('outcome', ['timed_out', 'cancelled', 'failed'])
def test_best_route_retained_on_deadline_cancellation_and_optimizer_failure(outcome):
    from app.planning_control import search_progress
    ready, resume = Event(), Event()
    manager = RouteJobManager(workers=1)

    def compute(_, publish):
        publish(candidate())
        search_progress({'forward': [{'points': [[118, 31], [118.5, 31.5]]}]})
        ready.set()
        assert resume.wait(3)
        if outcome == 'failed':
            raise ValueError('optimizer failed')
        checkpoint()
        return candidate(90)

    job = manager.submit(compute, None)
    try:
        assert ready.wait(3)
        if outcome == 'timed_out':
            with manager.lock:
                manager.jobs[job['job_id']].deadline = monotonic() - 1
        if outcome == 'cancelled':
            manager.cancel_job(job['job_id'])
        resume.set()
        manager.jobs[job['job_id']].future.result(3)
        final = manager.snapshot(job['job_id'])
        assert final['status'] == outcome
        assert final['result']['distance_km'] == 100
        assert final['search_progress'] is None and final['progress_revision'] >= 2
    finally:
        resume.set()
        manager.shutdown()


def test_distance_priority_and_reference_route_ranking():
    manager = RouteJobManager(workers=1)

    def compute(_, publish):
        reference = candidate(10, allowed=False)
        reference['wind_shear_fallback'] = {'active': True}
        publish(reference)
        assert manager.snapshot(job_id[0])['result'] is None
        for distance, wind in [(100, 1), (90, 5)]:
            route = candidate(distance, wind)
            route['planning_strategy'] = 'distance_priority'
            publish(route)
        return reference

    # Coordinate publication after submit returns its ID.
    start = Event()
    job_id = []
    def run(request, publish):
        assert start.wait(3)
        return compute(request, publish)
    job = manager.submit(run, None)
    job_id.append(job['job_id'])
    try:
        start.set()
        manager.jobs[job['job_id']].future.result(3)
        result = manager.snapshot(job['job_id'])
        assert result['revision'] == 2
        assert result['feasible']
        assert result['result']['distance_km'] == 90
    finally:
        start.set()
        manager.shutdown()


def test_queue_capacity_and_queued_cancellation():
    ready, resume = Event(), Event()
    manager = RouteJobManager(workers=1, capacity=2)
    def compute(_, publish):
        ready.set()
        assert resume.wait(3)
        return candidate()
    first = manager.submit(compute, None)
    try:
        assert ready.wait(3)
        second = manager.submit(compute, None)
        assert second['status'] == 'queued'
        with pytest.raises(PlanningQueueFull):
            manager.submit(compute, None)
        assert manager.cancel_job(second['job_id'])['status'] == 'cancelled'
        assert manager.jobs[second['job_id']].future.cancelled()
    finally:
        resume.set()
        manager.shutdown()


@pytest.mark.parametrize('planner_type', ['astar', 'lpa_star', 'wa_lpa_star'])
def test_searches_obey_cancellation_and_deadlines(planner_type):
    from app.main import plan_single_altitude_route
    from app.models import Thresholds
    from app.wind_provider import WindGrid
    grid = WindGrid(lons=np.arange(4.), lats=np.arange(4.)[::-1], u=np.ones((4, 4)), v=np.zeros((4, 4)),
                    cycle='test', forecast_hour=1, level='100m AGL', valid_time='test')
    cancel = Event()
    with pytest.raises(PlanningCancelled):
        with planning_scope(monotonic() + 30, cancel):
            cancel.set()
            plan_single_altitude_route(grid, (0., 3.), (3., 0.), Thresholds(), {}, planner_type)
    with planning_scope(monotonic() + 30, Event()):
        with pytest.raises(PlanningStageExpired):
            with stage_budget(-1):
                checkpoint()
        checkpoint()  # Expiring the preview does not expire the whole job.
    with pytest.raises(PlanningTimedOut):
        with planning_scope(monotonic() - 1, Event()):
            checkpoint()


def test_job_api_publishes_before_worker_finishes_and_supports_cancel(monkeypatch):
    from app import main
    manager = RouteJobManager(workers=1)
    ready, resume = Event(), Event()
    def compute(_, publish):
        publish(candidate())
        ready.set()
        assert resume.wait(3)
        checkpoint()
        return candidate(90)
    monkeypatch.setattr(main, 'route_jobs', manager)
    monkeypatch.setattr(main, 'compute_route_plan', compute)
    client = TestClient(main.app)
    try:
        response = client.post('/api/route/plan/jobs', json={'start': [118, 31], 'end': [119, 32], 'cycle': 'test', 'forecast_hour': 1, 'level': '100m AGL'})
        assert response.status_code == 202
        job_id = response.json()['job_id']
        assert ready.wait(3)
        progress = client.get(f'/api/route/plan/jobs/{job_id}').json()
        assert progress['status'] == 'running'
        assert progress['result']['points']
        assert client.delete(f'/api/route/plan/jobs/{job_id}').json()['status'] == 'cancelled'
        assert client.get('/api/route/plan/jobs/absent').status_code == 404
    finally:
        resume.set()
        manager.shutdown()


def test_compute_publishes_constrained_preview_before_selected_planner(monkeypatch):
    from app import main
    from app.models import RoutePlanRequest
    from app.wind_provider import WindGrid
    grid = WindGrid(lons=np.arange(9.) * .25 + 118, lats=32 - np.arange(9.) * .25,
                    u=np.ones((9, 9)), v=np.zeros((9, 9)), cycle='test', forecast_hour=1,
                    level='250-350m AGL average', valid_time='test')
    grid.u[1:, 4] = 9.  # The preview must detour through the only safe gap.
    monkeypatch.setattr(main, 'load_grid', lambda *_: grid)
    monkeypatch.setenv('ROUTE_PLAN_MULTI_ALTITUDE', '0')
    published = []
    original = main.compare_forward_reverse_routes
    def optimize(*args, **kwargs):
        assert published
        assert published[0]['planner_type'] == 'astar_preview'
        assert published[0]['requested_planner_type'] == 'wa_lpa_star'
        return original(*args, **kwargs)
    monkeypatch.setattr(main, 'compare_forward_reverse_routes', optimize)
    request = RoutePlanRequest(start=(118, 31), end=(120, 31), cycle='test', forecast_hour=1,
                               level=grid.level, wind_shear={'enabled': False})
    with planning_scope(monotonic() + 10, Event()):
        final = main.compute_route_plan(request, publish=published.append)
    assert final['analysis']['navigation_allowed']
    assert len(published) >= 2
    assert all(route['analysis']['max_wind_speed'] < request.thresholds.danger for route in published)
    assert all(point[3] == 300 for point in published[0]['points'])
    assert max(point[1] for point in published[0]['points']) == 32


def test_shear_disabled_reference_is_never_published_as_preview(monkeypatch):
    from app import main
    from app.models import RoutePlanRequest
    from app.wind_provider import WindGrid
    grid = WindGrid(lons=np.array([118., 118.25]), lats=np.array([31., 30.75]),
                    u=np.array([[1., 5.], [1., 5.]]), v=np.zeros((2, 2)),
                    cycle='test', forecast_hour=1, level='100m AGL', valid_time='test')
    monkeypatch.setattr(main, 'load_grid', lambda *_: grid)
    monkeypatch.setenv('ROUTE_PLAN_MULTI_ALTITUDE', '0')
    request = RoutePlanRequest(start=(118, 31), end=(118.25, 31), cycle='test', forecast_hour=1,
                               level=grid.level, wind_shear={'horizontal': {'hard_delta_wind_vector_ms': 1}})
    published = []
    result = main.compute_route_plan(request, publish=published.append)
    assert result['wind_shear_fallback']['active']
    assert result['analysis']['navigation_allowed'] is False
    assert not published
