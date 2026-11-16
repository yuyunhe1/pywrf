from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

from app.route_jobs import RouteJobManager
from app.planning_control import checkpoint
from test_route_jobs import candidate


def wait_for(condition, seconds=2):
    finish = monotonic() + seconds
    while not condition():
        assert monotonic() < finish
        sleep(.005)


def test_independent_monitor_cancels_lost_owner_without_polling():
    manager = RouteJobManager(workers=1, owner_timeout_seconds=.05, monitor_interval=.005)
    ready = Event()
    def compute(_, publish):
        publish(candidate())
        ready.set()
        while True:
            checkpoint()
            sleep(.005)
    state = manager.submit(compute, SimpleNamespace(client_instance_id='page-A'))
    try:
        assert ready.wait(2)
        job = manager.jobs[state['job_id']]
        wait_for(lambda: job.future.done())
        assert job.status == 'cancelled' and job.result['points']
        assert job.search_progress is None
    finally:
        manager.shutdown()


def test_queued_owner_expires_and_wrong_or_old_connection_cannot_renew_it():
    manager = RouteJobManager(workers=1, owner_timeout_seconds=.08, monitor_interval=.005)
    hold = Event()
    first = manager.submit(lambda _, publish: hold.wait(2) or candidate(), None)
    second = manager.submit(lambda _, publish: candidate(), SimpleNamespace(client_instance_id='page-B'))
    try:
        identifier = second['job_id']
        assert manager.attach_owner(identifier, 'wrong-page') is None
        generation = manager.attach_owner(identifier, 'page-B')
        newest = manager.attach_owner(identifier, 'page-B')
        assert not manager.touch_owner(identifier, 'page-B', generation)
        assert manager.touch_owner(identifier, 'page-B', newest)
        # Status readers cannot keep a disconnected page alive.
        wait_for(lambda: manager.snapshot(identifier)['status'] == 'cancelled')
        assert manager.jobs[identifier].future.cancelled()
        assert manager.jobs[first['job_id']].status == 'running'
    finally:
        hold.set()
        manager.shutdown()


def test_background_owner_heartbeats_keep_only_its_task_alive():
    manager = RouteJobManager(workers=2, owner_timeout_seconds=.1, monitor_interval=.005)
    def compute(_, publish):
        while True:
            checkpoint()
            sleep(.005)
    active = manager.submit(compute, SimpleNamespace(client_instance_id='background-page'))
    lost = manager.submit(compute, SimpleNamespace(client_instance_id='closed-page'))
    try:
        generation = manager.attach_owner(active['job_id'], 'background-page')
        for _ in range(12):
            assert manager.touch_owner(active['job_id'], 'background-page', generation)
            sleep(.02)
        assert manager.snapshot(active['job_id'])['status'] == 'running'
        assert manager.jobs[lost['job_id']].future.done()
        assert manager.snapshot(lost['job_id'])['status'] == 'cancelled'
    finally:
        manager.shutdown()
