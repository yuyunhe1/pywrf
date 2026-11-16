import json
import os
import time
from pathlib import Path
from threading import Event

import numpy as np
import pytest

from app import wrf_cache_provider as provider


@pytest.fixture(autouse=True)
def reset_sync_state(monkeypatch):
    monkeypatch.setenv("WRF_CACHE_AUTO_SYNC_INDEX", "0")
    monkeypatch.setattr(provider, "INDEX_SYNC_THREAD", None)
    monkeypatch.setattr(provider, "INDEX_SYNC_LAST_STARTED", None)
    monkeypatch.setattr(provider, "POLL_THREAD", None)
    monkeypatch.setattr(provider, "POLL_STOP", Event())
    monkeypatch.setattr(provider, "INDEX_SYNC_STATUS", {
        "running": False, "last_success": None, "last_error": None,
        "last_started": None, "next_check_at": None, "latest_cycle": None, "downloaded": 0,
    })
    provider.load_index.cache_clear()
    yield
    provider.stop_polling()
    for thread in (provider.INDEX_SYNC_THREAD, provider.POLL_THREAD):
        if thread is not None:
            thread.join(timeout=1)
    provider.load_index.cache_clear()


def test_wrf_cache_provider_reads_npz_grid(tmp_path, monkeypatch):
    cache_dir = tmp_path / "wrf_cache"
    run_dir = cache_dir / "2026062000"
    run_dir.mkdir(parents=True)
    np.savez_compressed(
        run_dir / "wrf_d02_2026062000_f001.npz",
        lons=np.asarray([117.0, 117.01, 117.02], dtype=np.float32),
        lats=np.asarray([32.0, 31.99], dtype=np.float32),
        levels_m=np.asarray([10, 100], dtype=np.float32),
        u=np.asarray([[[1, 2, 3], [4, 5, 6]], [[10, 20, 30], [40, 50, 60]]], dtype=np.float32),
        v=np.asarray([[[2, 3, 4], [5, 6, 7]], [[11, 21, 31], [41, 51, 61]]], dtype=np.float32),
        cycle_utc=np.asarray("2026-06-20 00:00 UTC"),
        valid_time_utc=np.asarray("2026-06-20 01:00 UTC"),
        forecast_hour=np.asarray(1, dtype=np.int16),
    )
    (cache_dir / "index.json").write_text(
        json.dumps(
            {
                "levels": ["10m AGL", "100m AGL"],
                "valid_times": [
                    {
                        "label": "2026-06-20 09:00 北京时间",
                        "valid_time": "2026-06-20 01:00 UTC",
                        "cycle": "2026-06-20 00:00 UTC",
                        "cycle_bj": "2026-06-20 08:00 北京时间",
                        "forecast_hour": 1,
                    },
                    {
                        "label": "2026-06-20 10:00 北京时间",
                        "valid_time": "2026-06-20 02:00 UTC",
                        "cycle": "2026-06-20 00:00 UTC",
                        "cycle_bj": "2026-06-20 08:00 北京时间",
                        "forecast_hour": 2,
                    },
                ],
                "files": [
                    {
                        "cycle": "2026-06-20 00:00 UTC",
                        "cycle_bj": "2026-06-20 08:00 北京时间",
                        "forecast_hour": 1,
                        "valid_time": "2026-06-20 01:00 UTC",
                        "valid_time_bj": "2026-06-20 09:00 北京时间",
                        "path": "2026062000/wrf_d02_2026062000_f001.npz",
                    },
                    {
                        "cycle": "2026-06-20 00:00 UTC",
                        "cycle_bj": "2026-06-20 08:00 北京时间",
                        "forecast_hour": 2,
                        "valid_time": "2026-06-20 02:00 UTC",
                        "valid_time_bj": "2026-06-20 10:00 北京时间",
                        "path": "2026062000/wrf_d02_2026062000_f002.npz",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("WRF_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("GFS_NOW_UTC", "2026-06-20 00:30 UTC")
    monkeypatch.delenv("WRF_CACHE_REMOTE_HOST", raising=False)
    monkeypatch.delenv("WRF_CACHE_REMOTE_DIR", raising=False)
    provider.load_index.cache_clear()
    provider._get_grid_cached.cache_clear()

    available = provider.availability()
    assert available["levels"] == ["10m AGL", "100m AGL"]
    assert available["valid_times"][0]["label"] == "2026-06-20 09:00 北京时间"
    assert available["forecast_hours"] == [1]

    grid = provider.get_grid_by_valid_time("2026-06-20 09:00 北京时间", "100m AGL", None)
    assert grid.source.startswith("WRF cache")
    assert grid.u.shape == (2, 3)
    assert grid.u[0, 1] == 20
    assert grid.v[1, 2] == 61

    cropped = provider.get_grid("2026-06-20 00:00 UTC", 1, "10m", (117.0, 31.99, 117.01, 32.0))
    assert cropped.u.shape == (2, 2)
    assert cropped.level == "10m AGL"

    os.environ.pop("GFS_NOW_UTC", None)


def _write_index(cache_dir: Path) -> None:
    records = [
        {
            "label": "2026-07-13 09:00 北京时间",
            "valid_time": "2026-07-13 01:00 UTC",
            "cycle": "2026-07-13 00:00 UTC",
            "cycle_bj": "2026-07-13 08:00 北京时间",
            "forecast_hour": 1,
            "path": "2026071300/local.npz",
        },
        {
            "label": "2026-07-20 09:00 北京时间",
            "valid_time": "2026-07-20 01:00 UTC",
            "cycle": "2026-07-20 00:00 UTC",
            "cycle_bj": "2026-07-20 08:00 北京时间",
            "forecast_hour": 1,
            "path": "2026072000/remote-only.npz",
        },
    ]
    cache_dir.mkdir(parents=True)
    (cache_dir / "2026071300").mkdir()
    (cache_dir / "2026071300" / "local.npz").touch()
    (cache_dir / "index.json").write_text(
        json.dumps({"files": records, "valid_times": records, "levels": ["100m AGL"]}),
        encoding="utf-8",
    )


def _configure_local_cache(monkeypatch, tmp_path: Path) -> Path:
    cache_dir = tmp_path / "wrf-cache"
    _write_index(cache_dir)
    monkeypatch.setenv("WRF_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("WRF_CACHE_REMOTE_HOST", "wrf.example")
    monkeypatch.setenv("WRF_CACHE_REMOTE_DIR", "/remote/wrf")
    monkeypatch.setenv("WRF_CACHE_EXPOSE_REMOTE_FILES", "0")
    provider.load_index.cache_clear()
    provider._get_grid_cached.cache_clear()
    return cache_dir


def test_availability_only_exposes_downloaded_files_by_default(monkeypatch, tmp_path):
    _configure_local_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("WRF_CACHE_AUTO_SYNC_INDEX", "0")

    result = provider.availability()

    assert result["remote_files_exposed"] is False
    assert [item["valid_time"] for item in result["valid_times"]] == ["2026-07-13 01:00 UTC"]


def test_remote_index_failure_does_not_block_local_availability(monkeypatch, tmp_path):
    _configure_local_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("WRF_CACHE_AUTO_SYNC_INDEX", "1")
    monkeypatch.setenv("WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS", "0")

    def fail_sync(force_index=True):
        raise RuntimeError("simulated SSH failure")

    monkeypatch.setattr(provider, "sync_latest_cycle", fail_sync)
    monkeypatch.setattr(provider, "INDEX_SYNC_THREAD", None)
    monkeypatch.setattr(provider, "INDEX_SYNC_LAST_STARTED", 0.0)
    provider.INDEX_SYNC_STATUS.update(running=False, last_success=None, last_error=None)

    started = time.perf_counter()
    result = provider.availability()
    elapsed = time.perf_counter() - started
    provider.INDEX_SYNC_THREAD.join(timeout=1.0)

    assert elapsed < 0.5
    assert result["valid_times"]
    assert provider.diagnostics()["remote_sync"]["last_error"] == "simulated SSH failure"


def test_remote_key_uses_documented_key_and_respects_override(monkeypatch, tmp_path):
    monkeypatch.delenv("WRF_CACHE_REMOTE_KEY", raising=False)
    monkeypatch.setattr(provider.Path, "home", lambda: tmp_path)
    key = tmp_path / ".ssh" / "gdex_proxy_ed25519"
    key.parent.mkdir()
    key.touch()
    assert provider._remote_key_filename() == str(key)
    monkeypatch.setenv("WRF_CACHE_REMOTE_KEY", "explicit-key")
    assert provider._remote_key_filename() == "explicit-key"


def test_new_remote_cycle_keeps_local_time_until_replacement_downloaded(monkeypatch, tmp_path):
    cache = _configure_local_cache(monkeypatch, tmp_path)
    index = json.loads((cache / "index.json").read_text())
    old, new = index["files"]
    old["forecast_hour"] = 169
    old["valid_time"] = new["valid_time"]
    old["label"] = new["label"]
    index["valid_times"] = [new]
    (cache / "index.json").write_text(json.dumps(index))
    provider.load_index.cache_clear()
    monkeypatch.setattr(provider, "_get_grid_cached", lambda path, *_: path)

    available = provider.availability()
    assert len(available["valid_times"]) == 1
    assert available["valid_times"][0]["cycle"] == old["cycle"]
    assert provider.get_grid_by_valid_time(new["valid_time"], "100m AGL", None) == old["path"]

    replacement = cache / new["path"]
    replacement.parent.mkdir()
    replacement.touch()
    assert provider.availability()["valid_times"][0]["cycle"] == new["cycle"]
    assert provider.get_grid_by_valid_time(new["valid_time"], "100m AGL", None) == new["path"]


@pytest.mark.parametrize("now, target", [
    ("2026-07-20 00:00 UTC", "2026-07-20 03:00 UTC"),
    ("2026-07-20 00:01 UTC", "2026-07-20 04:00 UTC"),
    ("2026-07-20 22:30 UTC", "2026-07-21 02:00 UTC"),
])
def test_lookahead_requires_local_product_at_current_time_plus_three_hours(monkeypatch, tmp_path, now, target):
    cache = _configure_local_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("GFS_NOW_UTC", now)
    files = provider.load_index()["files"]
    files[0]["forecast_hour"] = 3  # f003 from a stale cycle is insufficient.
    files[1]["valid_time"] = target
    result = provider.lookahead_status(files)
    assert result["target_valid_time"] == target
    assert result["available"] is False  # A remote index entry alone is insufficient.
    local = cache / files[1]["path"]
    local.parent.mkdir()
    local.touch()
    assert provider.lookahead_status(files)["available"] is True


def test_background_sync_downloads_new_cycle_and_prioritizes_lookahead(monkeypatch, tmp_path):
    cache = _configure_local_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("GFS_NOW_UTC", "2026-07-20 00:00 UTC")
    monkeypatch.setenv("WRF_CACHE_AUTO_SYNC_INDEX", "1")
    index = provider.load_index()
    index["files"].append({
        **index["files"][-1], "valid_time": "2026-07-20 03:00 UTC",
        "forecast_hour": 3, "path": "2026072000/lookahead.npz",
    })
    downloads = []

    def download(relative_path, force=False):
        downloads.append(relative_path)
        path = cache / relative_path
        path.parent.mkdir(exist_ok=True)
        if relative_path == provider.REMOTE_INDEX:
            path.write_text(json.dumps(index))
        else:
            path.touch()
        return path

    monkeypatch.setattr(provider, "_download_remote_file", download)
    provider.schedule_index_sync()
    provider.INDEX_SYNC_THREAD.join(timeout=1)
    assert not provider.INDEX_SYNC_THREAD.is_alive()
    assert downloads == ["index.json", "2026072000/lookahead.npz", "2026072000/remote-only.npz"]
    status = provider.diagnostics()["remote_sync"]
    assert status["downloaded"] == 2
    assert status["latest_cycle"] == "2026-07-20 00:00 UTC"
    assert provider.availability()["lookahead"]["available"] is True


def test_queries_are_throttled_to_ten_minutes_and_recover_after_failure(monkeypatch, tmp_path):
    _configure_local_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("WRF_CACHE_AUTO_SYNC_INDEX", "1")
    monkeypatch.delenv("WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS", raising=False)
    now = [1000.0]
    monkeypatch.setattr(provider, "monotonic", lambda: now[0])
    calls = []

    def sync(force_index=True):
        calls.append(force_index)
        if len(calls) == 1:
            raise RuntimeError("temporarily unavailable")
        return {"latest_cycle": "2026-07-20 00:00 UTC", "downloaded": 1}

    monkeypatch.setattr(provider, "sync_latest_cycle", sync)
    provider.schedule_index_sync()
    provider.INDEX_SYNC_THREAD.join(timeout=1)
    assert provider.diagnostics()["remote_sync"]["last_error"] == "temporarily unavailable"
    now[0] += 599
    provider.availability()
    assert len(calls) == 1
    now[0] += 1
    provider.availability()
    provider.INDEX_SYNC_THREAD.join(timeout=1)
    assert len(calls) == 2
    assert provider.diagnostics()["remote_sync"]["last_error"] is None


def test_first_start_does_not_block_and_sync_jobs_cannot_overlap(monkeypatch, tmp_path):
    monkeypatch.setenv("WRF_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WRF_CACHE_REMOTE_HOST", "wrf.example")
    monkeypatch.setenv("WRF_CACHE_REMOTE_DIR", "/remote/wrf")
    monkeypatch.setenv("WRF_CACHE_AUTO_SYNC_INDEX", "1")
    entered, release = Event(), Event()
    calls = []

    def sync(force_index=True):
        calls.append(True)
        entered.set()
        release.wait(timeout=1)
        return {}

    monkeypatch.setattr(provider, "sync_latest_cycle", sync)
    try:
        started = time.perf_counter()
        result = provider.availability()
        assert time.perf_counter() - started < 0.5
        assert result["valid_times"] == []
        assert result["lookahead"]["available"] is False
        assert entered.wait(timeout=1)
        first = provider.INDEX_SYNC_THREAD
        provider.schedule_index_sync(force=True)
        assert provider.INDEX_SYNC_THREAD is first
        assert len(calls) == 1
    finally:
        release.set()
        provider.INDEX_SYNC_THREAD.join(timeout=1)


def test_poll_loop_queries_without_browser_and_stops(monkeypatch):
    monkeypatch.delenv("WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS", raising=False)
    monkeypatch.setattr(provider, "monotonic", lambda: 1000)
    monkeypatch.setattr(provider, "INDEX_SYNC_LAST_STARTED", 1000)
    waits, calls = [], []

    class Stop:
        def is_set(self):
            return False

        def wait(self, delay):
            waits.append(delay)
            return len(waits) == 2

    monkeypatch.setattr(provider, "schedule_index_sync", lambda: calls.append(True))
    provider._poll_remote_cache(Stop())
    assert waits == [600, 600]
    assert calls == [True]


def test_incomplete_remote_index_keeps_previous_local_copy(monkeypatch, tmp_path):
    cache = _configure_local_cache(monkeypatch, tmp_path)
    previous = (cache / "index.json").read_text()
    closed = []

    class Sftp:
        def get(self, remote, local):
            Path(local).write_text('{"files": [')

        def close(self):
            closed.append("sftp")

    class Client:
        def close(self):
            closed.append("client")

    monkeypatch.setattr(provider, "_connect_sftp", lambda: (Client(), Sftp()))
    with pytest.raises(json.JSONDecodeError):
        provider.sync_index(force=True)
    assert (cache / "index.json").read_text() == previous
    assert not (cache / "index.json.tmp").exists()
    assert closed == ["sftp", "client"]
