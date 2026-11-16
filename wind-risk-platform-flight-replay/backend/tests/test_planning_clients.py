from threading import Event
from time import monotonic,sleep
from types import SimpleNamespace

import pytest

from app.planning_clients import PlanningClients,PlanningSuperseded
from app.planning_control import checkpoint,planning_scope
from app.route_jobs import RouteJobManager
from app.models import RoutePlanRequest
from test_route_jobs import candidate


def request(client='page-A',sequence=1):
    return SimpleNamespace(client_instance_id=client,client_request_sequence=sequence)


def wait_done(job):
    finish=monotonic()+2
    while not job.future.done():
        assert monotonic()<finish
        sleep(.005)


def test_pending_cancel_and_out_of_order_posts_cannot_create_a_superseded_search():
    manager=RouteJobManager(workers=1)
    pages=PlanningClients(manager)
    try:
        first=pages.begin(request(sequence=1))
        second=pages.begin(request(sequence=2))
        with pytest.raises(PlanningSuperseded): pages.submit(first,manager,lambda:pytest.fail('stale create'))
        pages.cancel('page-A',2)
        with pytest.raises(PlanningSuperseded): pages.submit(second,manager,lambda:pytest.fail('cancelled create'))
        assert pages.begin(request(sequence=3))==('page-A',3)
    finally: manager.shutdown()


def test_latest_request_cancels_across_managers_without_affecting_other_visitors():
    left,right=RouteJobManager(workers=2),RouteJobManager(workers=1)
    pages=PlanningClients(left,right)
    def compute(_,publish):
        publish(candidate())
        while True: checkpoint();sleep(.002)
    try:
        token=pages.begin(request())
        state=pages.submit(token,left,lambda:left.submit(compute,request()))
        other=left.submit(compute,request('other-page',1))
        first=left.jobs[state['job_id']]
        while not first.result:sleep(.002)
        new=pages.begin(request(sequence=2))
        second=pages.submit(new,right,lambda:right.submit(compute,request(sequence=2)))
        wait_done(first)
        assert first.status=='cancelled' and first.result is None
        assert left.snapshot(other['job_id'])['status']=='running'
        assert right.snapshot(second['job_id'])['status']=='running'
        pages.cancel('page-A',2,forget=True)
        wait_done(right.jobs[second['job_id']])
        assert right.jobs[second['job_id']].result is None
    finally:left.shutdown();right.shutdown()


def test_identical_post_retry_uses_one_job_and_legacy_requests_do_not_supersede():
    manager=RouteJobManager(workers=1)
    pages=PlanningClients(manager)
    try:
        token=pages.begin(request())
        first=pages.submit(token,manager,lambda:manager.submit(lambda _,publish:candidate(),request()))
        retry=pages.submit(pages.begin(request()),manager,lambda:pytest.fail('duplicate job'))
        assert retry['job_id']==first['job_id']
        assert pages.begin(request(sequence=None)) is None
    finally:manager.shutdown()


def test_seed_routes_cannot_change_endpoints_or_exceed_request_limits():
    body=dict(start=[118,30],end=[119,31],source='wrf',level='100m AGL',time_aware=True)
    assert RoutePlanRequest(**body,seed_routes=[[[118,30],[118.3,30.6],[119,31]]]).seed_routes
    for route in ([[117,30],[119,31]],[[118,30],[119,32]],[[118,30]]):
        with pytest.raises(ValueError):RoutePlanRequest(**body,seed_routes=[route])


def test_lost_page_owner_releases_payloads_without_waiting_for_a_new_request():
    manager=RouteJobManager(workers=1,owner_timeout_seconds=.03,monitor_interval=.005)
    def compute(_,publish):
        publish(candidate())
        while True: checkpoint();sleep(.002)
    try:
        state=manager.submit(compute,request())
        job=manager.jobs[state['job_id']]
        wait_done(job)
        assert job.status=='cancelled' and job.result is None
    finally:manager.shutdown()
