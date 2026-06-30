import json
import os

import numpy as np


def test_wrf_cache_provider_reads_npz_grid(tmp_path, monkeypatch):
    from app import wrf_cache_provider

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
                    }
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
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("WRF_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("GFS_NOW_UTC", "2026-06-20 00:30 UTC")
    monkeypatch.delenv("WRF_CACHE_REMOTE_HOST", raising=False)
    monkeypatch.delenv("WRF_CACHE_REMOTE_DIR", raising=False)
    wrf_cache_provider.load_index.cache_clear()
    wrf_cache_provider._get_grid_cached.cache_clear()

    available = wrf_cache_provider.availability()
    assert available["levels"] == ["10m AGL", "100m AGL"]
    assert available["valid_times"][0]["label"] == "2026-06-20 09:00 北京时间"
    assert available["forecast_hours"] == [1]

    grid = wrf_cache_provider.get_grid_by_valid_time("2026-06-20 09:00 北京时间", "100m AGL", None)
    assert grid.source.startswith("WRF cache")
    assert grid.u.shape == (2, 3)
    assert grid.u[0, 1] == 20
    assert grid.v[1, 2] == 61

    cropped = wrf_cache_provider.get_grid("2026-06-20 00:00 UTC", 1, "10m", (117.0, 31.99, 117.01, 32.0))
    assert cropped.u.shape == (2, 2)
    assert cropped.level == "10m AGL"

    os.environ.pop("GFS_NOW_UTC", None)
