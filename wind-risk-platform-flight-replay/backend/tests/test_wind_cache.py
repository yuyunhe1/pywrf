from concurrent.futures import ThreadPoolExecutor
from threading import Event

import numpy as np

from app.wind_cache import bounded_grid_cache, grid_bytes


def test_cache_evicts_by_bytes_and_reuses_recent_frames():
    calls = []
    @bounded_grid_cache(8, maxsize=10, size_of=len)
    def load(key):
        calls.append(key)
        return b'1234'
    assert load('a') == load('b') == load('a')
    load('c')
    assert calls == ['a', 'b', 'c']
    assert load.cache_stats()['bytes'] == 8
    load('b')
    assert calls[-1] == 'b'
    assert load.cache_stats()['entries'] == 2


def test_oversized_frame_is_served_without_retaining_it():
    @bounded_grid_cache(2, size_of=len)
    def load(key): return b'1234'
    assert load('large') == b'1234'
    assert load.cache_stats()['bytes'] == 0


def test_parallel_requests_decode_the_same_frame_only_once():
    began, finish = Event(), Event()
    calls = []
    @bounded_grid_cache(10, size_of=len)
    def load(key):
        calls.append(key)
        began.set()
        assert finish.wait(2)
        return b'1234'
    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(load, 'frame')
        assert began.wait(2)
        two = executor.submit(load, 'frame')
        finish.set()
        assert one.result() == two.result() == b'1234'
    assert calls == ['frame']
    assert load.cache_stats()['bytes'] == 4


def test_invalidated_inflight_frame_cannot_repopulate_cache():
    began, finish = Event(), Event()
    @bounded_grid_cache(10, size_of=len)
    def load(key):
        began.set()
        assert finish.wait(2)
        return b'1234'
    with ThreadPoolExecutor(max_workers=1) as executor:
        value = executor.submit(load, 'old')
        assert began.wait(2)
        load.cache_clear()
        finish.set()
        assert value.result() == b'1234'
    assert load.cache_stats()['entries'] == 0


def test_byte_accounting_charges_a_view_for_its_retained_parent():
    from types import SimpleNamespace
    array = np.ones((100, 100))
    grid = SimpleNamespace(u=array[:2, :2], v=array[:2, :2], terrain=None)
    assert grid_bytes(grid) == array.nbytes
