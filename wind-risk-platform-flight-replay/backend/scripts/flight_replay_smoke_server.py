"""Isolated WRF/route fixture for the browser acceptance script."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]
WORK = ROOT / "data" / "flight_replay_tests" / "browser"
CACHE = WORK / "wrf"
CACHE.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BACKEND))
cycle = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
os.environ.update(GFS_AUTO_DOWNLOAD="0", WRF_CACHE_AUTO_SYNC_INDEX="0", WRF_CACHE_SYNC_ON_STARTUP="0",
                  WRF_CACHE_REMOTE_HOST="", WRF_CACHE_DIR=str(CACHE),
                  GFS_NOW_UTC=(cycle + timedelta(hours=1, minutes=10)).strftime("%Y-%m-%d %H:%M UTC"),
                  MAP_TILE_CACHE_DIR=str(ROOT / "data/map_tile_cache"))
records = []
for hour in range(9):
    path = f"smoke/h{hour}.npz"
    (CACHE / path).parent.mkdir(exist_ok=True)
    valid = cycle + timedelta(hours=hour)
    lons, lats = np.linspace(118.4, 119.6, 61), np.linspace(31.4, 31.0, 21)
    u = np.full((2, len(lats), len(lons)), 6.0)
    if hour == 1:
        u[:, :, 45:] = 10
    np.savez_compressed(CACHE / path, lons=lons, lats=lats, levels_m=np.array([100, 300]), u=u,
                        v=np.zeros_like(u), cycle_utc=np.array(cycle.strftime("%Y-%m-%d %H:%M UTC")),
                        valid_time_utc=np.array(valid.strftime("%Y-%m-%d %H:%M UTC")), forecast_hour=np.array(hour))
    records.append({"cycle": cycle.strftime("%Y-%m-%d %H:%M UTC"), "valid_time": valid.strftime("%Y-%m-%d %H:%M UTC"),
                    "forecast_hour": hour, "path": path})
(CACHE / "index.json").write_text(json.dumps({"files": records, "levels": ["100m AGL", "300m AGL"]}), encoding="utf-8")
from app import main, route_storage
route_storage.DB_PATH = WORK / "routes.sqlite3"
for identifier, name, points in (
    ("replay-a", "回放验收 A", [[118.45, 31.1], [118.7, 31.17], [119.4, 31.1]]),
    ("replay-b", "回放验收 B", [[118.45, 31.1], [118.75, 31.1]]),
):
    route_storage.save_route({"name": name, "points": points, "start": points[0], "end": points[-1], "level": "100m AGL"}, identifier)
(WORK / "fixture.json").write_text(json.dumps({"cycle": cycle.isoformat(), "cache_dir": str(CACHE)}), encoding="utf-8")
import uvicorn
uvicorn.run(main.app, host="127.0.0.1", port=int(os.getenv("REPLAY_SMOKE_PORT", "8002")))
