"""One cancellable cache preparer; foreground frames always take priority."""
from collections import OrderedDict
from contextlib import contextmanager
from threading import Condition, Event, Thread
import os
from time import monotonic

import numpy as np

from .planning_limits import memory_pressure

_condition = Condition()
_pending = OrderedDict()
_finished = OrderedDict()
_stop = Event()
_thread = None
_foreground = 0
_state = dict(running=False, prepared_frames=0, errors=0)


@contextmanager
def foreground_weather():
    global _foreground
    with _condition:
        _foreground += 1
    try:
        yield
    finally:
        with _condition:
            _foreground -= 1
            _condition.notify_all()


def schedule(level='100m AGL', cycle=None, after=None):
    from . import wrf_cache_provider as wrf
    from .wrf_native_schema import native_required
    global _thread
    if os.getenv('WIND_FRAME_WARMUP', '1') != '1' or not native_required():
        return
    try:
        records = wrf.load_index().get('files', [])
        cycle = cycle or max((record['cycle'] for record in records), default=None)
        rows = sorted((row for row in records if row['cycle'] == cycle), key=lambda row: row['valid_time'])
        if level != '100m AGL' or after is not None:
            rows = [row for row in rows if after is None or row['valid_time'] >= after][:3]
        if not rows:
            return
        key = (cycle, level, tuple((row['path'], row.get('sha256')) for row in rows))
        with _condition:
            if key in _finished or key in _pending or _state.get('key') == key:
                return
            _pending[key] = (rows, level)
            while len(_pending) > 4:
                _pending.popitem(last=False)
            if _thread is None or not _thread.is_alive():
                _stop.clear()
                _thread = Thread(target=_run, name='wind-cache-warmup', daemon=True)
                _thread.start()
            _condition.notify_all()
    except (OSError, ValueError, KeyError):
        return  # Selection/data errors are reported by the foreground request.


def _run():
    from .forecast_store import IOStats, wrf_product
    while not _stop.is_set():
        with _condition:
            if not _pending:
                _state.update(running=False, key=None)
                _condition.wait(.5)
                continue
            key, (rows, level) = _pending.popitem(last=False)
            _state.update(running=True, key=key, cycle=key[0], level=level, prepared_frames=0,
                          total_frames=len(rows), errors=0)
        began, io, complete = monotonic(), IOStats(), True
        for row in rows:
            with _condition:
                while _foreground and not _stop.is_set():
                    _condition.wait(.05)
            if _stop.is_set() or memory_pressure():
                complete = False
                break
            try:
                grid, _ = wrf_product(row, level, io)
                np.asarray(grid.u); np.asarray(grid.v)
                with _condition:
                    _state['prepared_frames'] += 1
            except (OSError, ValueError, KeyError, EOFError):
                complete = False
                with _condition:
                    _state['errors'] += 1
        with _condition:
            _state.update(running=False, key=None, elapsed_seconds=round(monotonic() - began, 3), io=io.snapshot())
            if complete:
                _finished[key] = True
                while len(_finished) > 32:
                    _finished.popitem(last=False)


def status():
    with _condition:
        return {key: value for key, value in _state.items() if key != 'key'} | {'queued': len(_pending), 'workers': 1}


def shutdown():
    _stop.set()
    with _condition:
        _pending.clear()
        _condition.notify_all()
    if _thread:
        _thread.join(timeout=2)
