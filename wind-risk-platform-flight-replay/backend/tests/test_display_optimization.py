"""Comparison lasts through the display and completes without cancellation."""
from threading import Event
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from app import main
from app.direction_pool import direction_pool, shutdown_direction_pool
from app.planning_control import checkpoint
from app.recommendations import RecommendationJobManager
from app.route_jobs import RouteJobManager
from app.temporal_plan_service import compute_temporal_plan
from test_temporal_routing import dataset, request, BASE
from test_recommendations import job_request, plan


def settled(manager, identity):
    deadline = monotonic() + 3
    while True:
        state = manager.snapshot(identity)
        if state['compute_finished']: return state
        assert monotonic() < deadline
        sleep(.005)


def test_finish_keeps_the_improved_route_and_does_not_cancel():
    req = request(optimize_during_display=True)
    data = dataset()
    ready, improve, improved = Event(), Event(), Event()
    def compute(request, publish):
        value = plan(request, data)
        publish(value); ready.set()
        while not improve.is_set(): checkpoint(); sleep(.001)
        publish({**value, 'points': [request.start, (.2,.1), request.end], 'objective_cost': 80.})
        improved.set()
        while True: checkpoint(); sleep(.001)
    manager = RouteJobManager(workers=1, timeout_seconds=30)
    try:
        identity = manager.submit(compute, req)['job_id']
        assert ready.wait(3)
        first_deadline = manager.jobs[identity].comparison_deadline
        assert first_deadline is not None
        improve.set(); assert improved.wait(3)
        assert manager.jobs[identity].comparison_deadline >= first_deadline
        manager.finish_job(identity)
        state = settled(manager, identity)
        assert state['status'] == 'completed' and state['feasible']
        assert state['result']['objective_cost'] == 80.
        assert state['result']['planning_termination'] == 'display_finished'
        assert not manager.jobs[identity].cancel.is_set()
        assert state['finish_requested']
    finally: manager.shutdown()


def test_unnotified_display_has_a_bounded_comparison_fallback():
    ready = Event()
    def compute(req, publish):
        publish(plan(req, dataset())); ready.set()
        while True: checkpoint(); sleep(.001)
    manager = RouteJobManager(workers=1, timeout_seconds=30)
    try:
        identity = manager.submit(compute, request(optimize_during_display=True))['job_id']
        assert ready.wait(3)
        manager.jobs[identity].comparison_deadline = monotonic() - 1
        state = settled(manager, identity)
        assert state['status'] == 'completed'
        assert state['result']['planning_termination'] == 'display_budget_elapsed'
    finally: manager.shutdown()


@pytest.mark.parametrize('kind', ['route', 'recommendation'])
def test_finish_api_rejects_missing_or_unverified_results(monkeypatch, kind):
    release = Event()
    def compute(req, publish):
        while not release.is_set(): checkpoint(); sleep(.001)
        return {'points': []}
    manager = RouteJobManager(workers=1, timeout_seconds=10)
    attribute, prefix = ('route_jobs', '/api/route/plan/jobs') if kind == 'route' else ('recommendation_jobs', '/api/route/recommendation/jobs')
    monkeypatch.setattr(main, attribute, manager)
    try:
        identity = manager.submit(compute, request())['job_id']
        client = TestClient(main.app)
        assert client.post(prefix+'/'+identity+'/finish').status_code == 409
        assert client.post(prefix+'/missing/finish').status_code == 404
        assert not manager.jobs[identity].finish_requested.is_set()
    finally: release.set(); manager.shutdown()


def test_recommendation_workflow_finishes_through_nested_scopes(monkeypatch):
    from app import recommendations
    monkeypatch.setattr(recommendations, '_current_utc', lambda: BASE)
    ready = Event()
    def compute(req, data, bbox, cells, publish, candidate_validator=None):
        publish(plan(req,data)); ready.set()
        while True: checkpoint(); sleep(.001)
    monkeypatch.setattr(recommendations, 'compute_temporal_plan', compute)
    manager = RecommendationJobManager(workers=1, timeout_seconds=10)
    try:
        identity = manager.submit_workflow(job_request(optimize_during_display=True), dataset())['job_id']
        assert ready.wait(3)
        manager.finish_job(identity)
        state = settled(manager, identity)
        assert state['status'] == 'completed'
        assert state['workflow']['stage'] == 'completed'
        assert len(state['workflow']['attempts']) == 1 and not state['workflow']['recommendations']
        assert not manager.jobs[identity].cancel.is_set()
    finally: manager.shutdown()


def test_finish_releases_real_parallel_workers_and_validation(monkeypatch):
    monkeypatch.setenv('ROUTE_PLAN_DIRECTION_PROCESSES', '2')
    monkeypatch.setenv('ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS', '0')
    ready = Event()
    req = request(optimize_during_display=True)
    manager = RouteJobManager(workers=1, timeout_seconds=20)
    def compute(request, publish):
        def accepted(value): publish(value); ready.set()
        return compute_temporal_plan(request, dataset(), [-.1,-.3,.7,.3], 0, accepted)
    try:
        identity = manager.submit(compute, req)['job_id']
        assert ready.wait(15)
        began = monotonic()
        manager.finish_job(identity)
        state = settled(manager, identity)
        assert monotonic()-began < 2
        assert state['status'] == 'completed' and state['feasible']
        assert direction_pool().cancel.is_set()
        assert direction_pool().lock.acquire(blocking=False)
        direction_pool().lock.release()
    finally: manager.shutdown(); shutdown_direction_pool()
