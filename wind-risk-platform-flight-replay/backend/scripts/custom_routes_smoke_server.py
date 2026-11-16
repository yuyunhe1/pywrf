"""Isolated UI acceptance service with deterministic hourly weather fixtures.

Real route persistence, export, continuous window evaluation and replay are used.
Only weather ingestion is replaced; this service never downloads or contacts SSH.
100m: safe; 200m: dangerous near hour 1 then safe; 300m: no safe windows;
500m: forecast unavailable. Never run against the platform's route database.
"""
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone

BACKEND = Path(__file__).resolve().parents[1]
WORK = Path(os.getenv('CUSTOM_ROUTE_TEST_DIR', str(BACKEND.parents[1] / 'data/custom_route_tests')))
WORK.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BACKEND))
os.environ.update(GFS_AUTO_DOWNLOAD='0', GFS_DATA_MODE='mock', WIND_DATA_MODE='mock',
                  WRF_CACHE_AUTO_SYNC_INDEX='0', WRF_CACHE_SYNC_ON_STARTUP='0', WRF_CACHE_REMOTE_HOST='',
                  WRF_V3_AUTO_SYNC='0', GFS_NOW_UTC='2026-11-05T00:30:00Z')

import numpy as np
from app import main, route_storage, departure_windows, flight_replay, wrf_timeline
from app.wind_provider import WindGrid

BASE = datetime(2026, 11, 5, tzinfo=timezone.utc)
CYCLE = BASE.strftime('%Y-%m-%d %H:%M UTC')
LEVELS = ['100m AGL', '200m AGL', '300m AGL', '500m AGL', '250-350m AGL average']
DATASETS = {}


def fixture_grid(hour, level, source):
    u = 10. if level == '300m AGL' else 12. if level == '200m AGL' and hour == 1 else 1. if level == '200m AGL' else 2.
    return WindGrid(np.linspace(117, 118.2, 25), np.linspace(31.2, 30.2, 21),
                    np.full((21, 25), u), np.zeros((21, 25)), CYCLE, hour, level,
                    (BASE + timedelta(hours=hour)).strftime('%Y-%m-%d %H:%M UTC'),
                    source=f'{source.upper()} UI acceptance fixture')


for source in ('gfs', 'wrf'):
    for level in LEVELS:
        identifier = f'fixture-{source}-{level.split("m")[0]}'
        frames = tuple(((BASE + timedelta(hours=hour)).timestamp(), fixture_grid(hour, level, source)) for hour in range(7))
        missing = level == '500m AGL'
        dataset = wrf_timeline.ForecastDataset(identifier, CYCLE, level, () if missing else frames,
                    BASE.timestamp(), (BASE + timedelta(hours=6)).timestamp(),
                    '验收夹具：缺少小时风场' if missing else None, source)
        DATASETS[source, level] = dataset
        wrf_timeline._DATASETS[identifier] = dataset


def availability(source=None, **_):
    return {'source': f'{(source or "gfs").upper()} UI acceptance fixture', 'cycles': [CYCLE],
            'forecast_hours': list(range(7)), 'levels': LEVELS, 'valid_times': [
                {'label': (BASE + timedelta(hours=hour)).strftime('%Y-%m-%d %H:%M UTC'),
                 'valid_time': (BASE + timedelta(hours=hour)).strftime('%Y-%m-%d %H:%M UTC'),
                 'cycle': CYCLE, 'forecast_hour': hour} for hour in range(7)],
            'remote_sync': {'enabled': False}, 'download': {'enabled': False, 'running': False}}


def load_grid(cycle=None, forecast_hour=None, level='100m AGL', bbox=None, valid_time=None, source=None):
    hour = int(forecast_hour or 0)
    if valid_time:
        hour = int((datetime.strptime(valid_time, '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc) - BASE).total_seconds() / 3600)
    return fixture_grid(hour, level, source or 'gfs')


for module in (departure_windows, flight_replay):
    module.load_dataset = lambda level, now: DATASETS['wrf', level]
    module.load_gfs_dataset = lambda level, now: DATASETS['gfs', level]
main.availability = availability
main.load_grid = load_grid
route_storage.DB_PATH = WORK / 'routes.sqlite3'
main.EXPORT_DIR = WORK / 'exports'
main.EXPORT_DIR.mkdir(exist_ok=True)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(main.app, host='127.0.0.1', port=int(os.getenv('CUSTOM_ROUTE_TEST_PORT', '8002')))
