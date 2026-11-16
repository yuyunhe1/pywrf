"""The GFS scheduler must work without requests, recover, and avoid overlap."""
import threading
from time import monotonic

import pytest

from app import gfs_downloader as gfs


@pytest.fixture
def scheduler(monkeypatch):
    gfs.stop_polling()
    monkeypatch.setenv('GFS_AUTO_DOWNLOAD', '1')
    monkeypatch.delenv('GFS_REALTIME_CHECK_INTERVAL_SECONDS', raising=False)
    monkeypatch.setattr(gfs, '_POLL_STATE', dict(next_check_at=None, last_check_at=None, poll_error=None))
    monkeypatch.setattr(gfs, '_STATE', dict(running=False))
    yield gfs
    gfs.stop_polling()


def test_default_interval_is_thirty_minutes_and_is_configurable(scheduler, monkeypatch):
    assert scheduler.check_interval_seconds() == 1800
    monkeypatch.setenv('GFS_REALTIME_CHECK_INTERVAL_SECONDS', '3600')
    assert scheduler.check_interval_seconds() == 3600
    monkeypatch.setenv('GFS_REALTIME_CHECK_INTERVAL_SECONDS', 'invalid')
    assert scheduler.check_interval_seconds() == 1800


def test_periodic_check_runs_without_browser_and_start_is_idempotent(scheduler, monkeypatch):
    called, times = threading.Event(), []
    monkeypatch.setattr(gfs, 'check_interval_seconds', lambda: .03)
    monkeypatch.setattr(gfs, 'start_realtime_download', lambda reason: (times.append(monotonic()), called.set()))
    began = monotonic()
    scheduler.start_polling(check_on_startup=False)
    worker = scheduler._POLL_WORKER
    scheduler.start_polling()
    assert scheduler._POLL_WORKER is worker
    assert called.wait(1)
    assert times[0] - began >= .02
    status = scheduler.status()
    assert status['polling'] and status['last_check_at'] and status['next_check_at']
    scheduler.stop_polling()
    assert not worker.is_alive()
    assert not scheduler.status()['polling']
    assert scheduler.status()['next_check_at'] is None


def test_scheduler_recovers_after_check_exception(scheduler, monkeypatch):
    recovered, calls = threading.Event(), []

    def check(reason):
        calls.append(reason)
        if len(calls) == 1:
            raise OSError('temporary failure')
        recovered.set()

    monkeypatch.setattr(gfs, 'check_interval_seconds', lambda: .02)
    monkeypatch.setattr(gfs, 'start_realtime_download', check)
    scheduler.start_polling()
    assert recovered.wait(1)
    assert scheduler.status()['poll_error'] is None


def test_scheduled_and_manual_checks_share_the_download_lock(scheduler, monkeypatch, tmp_path):
    started, release, calls = threading.Event(), threading.Event(), []

    def download(command, path):
        calls.append(command)
        started.set()
        release.wait(2)
        scheduler._finish(0, 'complete')

    monkeypatch.setattr(gfs, 'check_interval_seconds', lambda: .02)
    monkeypatch.setattr(gfs, '_build_command', lambda: ['isolated-test-downloader'])
    monkeypatch.setattr(gfs, 'log_dir', lambda: tmp_path)
    monkeypatch.setattr(gfs, '_run', download)
    try:
        scheduler.start_polling()
        assert started.wait(1)
        assert scheduler.start_realtime_download('manual')['running']
        threading.Event().wait(.075)
        assert len(calls) == 1
    finally:
        scheduler.stop_polling()
        release.set()
        if scheduler._WORKER:
            scheduler._WORKER.join(1)


def test_disabled_auto_download_does_not_start_scheduler(scheduler, monkeypatch):
    monkeypatch.setenv('GFS_AUTO_DOWNLOAD', '0')
    worker = scheduler._POLL_WORKER
    scheduler.start_polling()
    assert not scheduler.status()['polling']
    assert scheduler._POLL_WORKER is worker


@pytest.mark.parametrize(('mode', 'expected'), [('real', [True]), ('wrf_cache', [False]), ('mock', [])])
def test_backend_startup_registers_scheduler_independently_of_visitors(monkeypatch, mode, expected):
    from app import main
    calls = []
    monkeypatch.setattr(main, 'data_mode', lambda: mode)
    monkeypatch.setattr(main.gfs_downloader, 'start_polling', lambda check_on_startup: calls.append(check_on_startup))
    monkeypatch.setattr(main.wrf_cache_provider, 'start_polling', lambda **kwargs: None)
    monkeypatch.setattr(main.v3_native, 'start_polling', lambda: None)
    main.check_latest_gfs_on_startup()
    assert calls == expected
