"""Isolated browser acceptance service: real native V3, temporary route DB.

Only the unrelated GFS map is mocked. Native arrays, subprocess planning,
validation, fixed-path profiles, and departure windows use real WRF material.
"""
import os
from pathlib import Path
import sys

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
WORK=Path(os.getenv('V3_TEST_WORK_DIR',str(ROOT/'data/v3_integration_tests/browser')))
WORK.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(BACKEND))
os.environ.update(GFS_AUTO_DOWNLOAD='0',GFS_DATA_MODE='mock',WIND_DATA_MODE='mock',
    WRF_CACHE_AUTO_SYNC_INDEX='0',WRF_CACHE_SYNC_ON_STARTUP='0',WRF_CACHE_REMOTE_HOST='',WRF_V3_AUTO_SYNC='0',
    GFS_NOW_UTC='2026-11-04T02:00:00Z',WRF_V3_ARTIFACT_DIR=str(WORK/'runtime'),
    ROUTE_PLAN_TIMEOUT_SECONDS='22',WRF_V3_WINDOW_TIMEOUT_SECONDS='120',
    MAP_TILE_CACHE_DIR=str(ROOT/'data/map_tile_cache'))
from app import main,route_storage
route_storage.DB_PATH=WORK/'routes.sqlite3'
main.EXPORT_DIR=WORK/'exports'
main.EXPORT_DIR.mkdir(exist_ok=True)
import uvicorn
uvicorn.run(main.app,host='127.0.0.1',port=int(os.getenv('V3_SMOKE_PORT','8002')))
