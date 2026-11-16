"""Isolated UI service with real native data, jobs and a separate route store."""
import os
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
WORK = ROOT / 'data/planning_demo_tests/browser-storage'
WORK.mkdir(parents=True, exist_ok=True)
os.environ.update(GFS_AUTO_DOWNLOAD='0', WRF_CACHE_AUTO_SYNC_INDEX='0', WRF_CACHE_SYNC_ON_STARTUP='0',
    WRF_CACHE_REMOTE_HOST='', WRF_V3_AUTO_SYNC='0', WIND_FRAME_WARMUP='0',
    ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='2')

from app import main, route_storage
route_storage.DB_PATH = WORK / 'routes.sqlite3'
main.EXPORT_DIR = WORK / 'exports'
main.EXPORT_DIR.mkdir(exist_ok=True)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(main.app, host='127.0.0.1', port=8025)
