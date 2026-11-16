"""A page's first independently accepted route finishes, rather than cancels."""
from threading import Event
from time import monotonic, sleep

import pytest

from app.direction_pool import direction_pool, shutdown_direction_pool
from app.models import RecommendationJobRequest
from app.planning_control import planning_scope
from app.recommendations import RecommendationJobManager
from app.temporal_plan_service import compute_temporal_plan
from test_temporal_routing import dataset, request, profile, BASE


@pytest.fixture(scope='module', autouse=True)
def cleanup_workers():
    yield
    shutdown_direction_pool()


@pytest.mark.parametrize('processes', [0, 2])
@pytest.mark.parametrize('algorithm', ['astar', 'lpa_star', 'wa_lpa_star'])
def test_first_verified_route_finishes_and_releases_both_directions(monkeypatch, processes, algorithm):
    monkeypatch.setenv('ROUTE_PLAN_DIRECTION_PROCESSES', str(processes))
    data = dataset()
    req = request(planner_type=algorithm, stop_on_first_feasible=True)
    published = []
    with planning_scope(monotonic() + 20, Event()):
        result = compute_temporal_plan(req, data, [-.1, -.3, .7, .3], 0, published.append)
    assert len(published) == 1
    assert result['points'] == published[0]['points']
    assert profile(data, result, req)['analysis']['navigation_allowed']
    attempt = result['planning_diagnostics']['attempts'][0]
    assert attempt['stop_reason'] == 'first_feasible'
    assert attempt['complete_candidates'] == 1
    assert '搜索结束' in result['planning_message']
    if processes == 2:
        pool = direction_pool()
        assert pool.cancel.is_set()
        assert pool.lock.acquire(blocking=False), 'no task may retain the worker pair'
        pool.lock.release()
        assert attempt['worker_release_seconds'] < 2
        assert all(item['process'].is_alive() for item in pool.workers.values())


@pytest.mark.parametrize('processes', [0, 2])
def test_rejected_complete_candidate_does_not_end_search(monkeypatch, processes):
    monkeypatch.setenv('ROUTE_PLAN_DIRECTION_PROCESSES', str(processes))
    req = request(stop_on_first_feasible=True)
    req.seed_routes = [[req.start, req.end], [req.start, (.2, .1), req.end]]
    checks, published = [], []
    def independent_check(value):
        checks.append(value)
        assert len(checks) <= 2, 'search continued after its first accepted result'
        return len(checks) == 2
    with planning_scope(monotonic() + 20, Event()):
        result = compute_temporal_plan(req, dataset(), [-.1, -.3, .7, .3], 0,
            published.append, candidate_validator=independent_check)
    assert len(checks) == 2 and len(published) == 1
    assert result['analysis']['navigation_allowed']
    assert result['planning_diagnostics']['attempts'][0]['stop_reason'] == 'first_feasible'


def test_specified_workflow_completes_normally_without_future_attempts_or_optimization(monkeypatch):
    from app import recommendations
    monkeypatch.setattr(recommendations, '_current_utc', lambda: BASE)
    req = RecommendationJobRequest(**request(stop_on_first_feasible=True).model_dump())
    manager = RecommendationJobManager(workers=1, timeout_seconds=20)
    try:
        state = manager.submit_workflow(req, dataset())
        deadline = monotonic() + 20
        while not state['compute_finished']:
            assert monotonic() < deadline
            sleep(.01)
            state = manager.snapshot(state['job_id'])
        assert state['status'] == 'completed', state
        assert state['feasible'] and state['workflow']['stage'] == 'completed'
        assert len(state['workflow']['attempts']) == 1
        assert not state['workflow']['recommendations']
        assert state['result']['planning_diagnostics']['attempts'][0]['stop_reason'] == 'first_feasible'
        assert not manager.jobs[state['job_id']].cancel.is_set()
    finally:
        manager.shutdown()
