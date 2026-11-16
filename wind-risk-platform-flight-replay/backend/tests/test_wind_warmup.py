from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import numpy as np

from app import forecast_store, wind_warmup as warmup, wrf_cache_provider as wrf


def test_one_background_worker_waits_for_foreground_and_bounds_other_height_prefetch(monkeypatch):
    warmup.shutdown()
    cycle = 'isolated-' + uuid4().hex
    rows = [dict(cycle=cycle, path=f'frame-{i}', sha256=str(i), valid_time=f'{i:02d}') for i in range(8)]
    monkeypatch.setenv('WIND_FRAME_WARMUP', '1')
    monkeypatch.setenv('WRF_NATIVE_10MIN_REQUIRED', '1')
    monkeypatch.setattr(wrf, 'load_index', lambda: {'files': rows})
    monkeypatch.setattr(warmup, 'memory_pressure', lambda: None)
    calls, started, done = [], Event(), Event()
    def product(row, level, io):
        calls.append((row['path'], level))
        started.set()
        if len(calls) == 3: done.set()
        return SimpleNamespace(u=np.zeros((2, 2)), v=np.ones((2, 2))), row['sha256']
    monkeypatch.setattr(forecast_store, 'wrf_product', product)
    try:
        with warmup.foreground_weather():
            warmup.schedule('300m AGL', cycle, '02')
            assert not started.wait(.1)
        assert done.wait(2)
        assert calls == [('frame-2', '300m AGL'), ('frame-3', '300m AGL'), ('frame-4', '300m AGL')]
        until = monotonic() + 2
        while warmup.status()['running'] and monotonic() < until: sleep(.01)
        assert warmup.status()['workers'] == 1 and warmup.status()['prepared_frames'] == 3
        warmup.schedule('300m AGL', cycle, '02')
        assert warmup.status()['queued'] == 0
    finally:
        warmup.shutdown()
