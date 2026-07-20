import json
import os
import time
from pathlib import Path

import numpy as np

from app import wrf_cache_provider as provider


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

    def fail_sync(force=False):
        raise RuntimeError("simulated SSH failure")

    monkeypatch.setattr(provider, "sync_index", fail_sync)
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
