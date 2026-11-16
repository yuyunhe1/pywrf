"""Byte-bounded display-grid cache, coalescing concurrent frame decodes."""
from collections import OrderedDict, namedtuple
from concurrent.futures import Future
from functools import wraps
from threading import RLock, BoundedSemaphore

from .planning_control import checkpoint


def grid_cache_budget():
    from .planning_limits import integer_setting
    return integer_setting('WIND_GRID_CACHE_MB', 64) * 1024 ** 2


def decoded_cache_budget():
    return grid_cache_budget() * 3 // 4


def encoded_cache_budget():
    return grid_cache_budget() - decoded_cache_budget()


def grid_bytes(grid):
    # Charge views at their logical size too: an eviction must not accidentally
    # retain a much larger decoded parent through a cropped view.
    seen = set()
    total = 0
    for name in ('lons', 'lats', 'u', 'v', 'terrain'):
        array = getattr(grid, name, None)
        while getattr(array, 'base', None) is not None:
            array = array.base
        if array is not None and id(array) not in seen:
            seen.add(id(array))
            total += getattr(array, 'nbytes', 0)
    return total


def bounded_grid_cache(max_bytes, maxsize=48, decoders=2, size_of=grid_bytes, key_of=None):
    def decorate(load):
        lock, slots = RLock(), BoundedSemaphore(decoders)
        cached, inflight = OrderedDict(), {}
        byte_count = hits = misses = coalesced = epoch = 0

        @wraps(load)
        def wrapped(*args, **kwargs):
            nonlocal byte_count, hits, misses, coalesced
            key = key_of(args, kwargs) if key_of else (args, tuple(sorted(kwargs.items())))
            with lock:
                if key in cached:
                    hits += 1
                    cached.move_to_end(key)
                    return cached[key][0]
                flight_key = epoch, key
                current_epoch = epoch
                if flight_key in inflight:
                    future, owner = inflight[flight_key], False
                    coalesced += 1
                else:
                    future, owner = Future(), True
                    inflight[flight_key] = future
                    misses += 1
            if not owner:
                # Waiting for another HTTP request must remain cancellable when
                # the caller is a planning worker with a deadline.
                from concurrent.futures import TimeoutError
                while True:
                    checkpoint()
                    try:
                        return future.result(timeout=.05)
                    except TimeoutError:
                        if future.done():
                            return future.result()
            try:
                while not slots.acquire(timeout=.05):
                    checkpoint()
                try:
                    checkpoint()
                    value = load(*args, **kwargs)
                finally:
                    slots.release()
                size = size_of(value)
                with lock:
                    if current_epoch == epoch and size <= max_bytes and max_bytes > 0:
                        cached[key] = value, size
                        byte_count += size
                        while len(cached) > maxsize or byte_count > max_bytes:
                            _, (_, removed) = cached.popitem(last=False)
                            byte_count -= removed
                future.set_result(value)
                return value
            except BaseException as exc:
                future.set_exception(exc)
                raise
            finally:
                with lock:
                    inflight.pop(flight_key, None)

        def clear():
            nonlocal byte_count, hits, misses, coalesced, epoch
            with lock:
                cached.clear()
                byte_count = hits = misses = coalesced = 0
                epoch += 1  # An old in-flight response cannot repopulate a cleared version.

        def stats():
            with lock:
                return dict(entries=len(cached), bytes=byte_count, max_bytes=max_bytes,
                            hits=hits, misses=misses, coalesced=coalesced, inflight=len(inflight))

        wrapped.cache_clear, wrapped.cache_stats = clear, stats
        info = namedtuple('CacheInfo', 'hits misses maxsize currsize')
        wrapped.cache_info = lambda: info(hits, misses, maxsize, len(cached))
        return wrapped
    return decorate
