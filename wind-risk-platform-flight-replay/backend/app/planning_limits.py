"""Search budgets and available-memory checks without optional dependencies."""
import math
import os
import sys
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 7200
DEFAULT_MAX_GRID_CELLS = 150000
DEFAULT_MAX_TEMPORAL_LABELS = 600000
DEFAULT_MEMORY_RESERVE_MB = 2048
DEFAULT_PROCESS_MEMORY_MB = 2048


def integer_setting(name, default, minimum=0):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def seconds_setting(name, default):
    try:
        value = float(os.getenv(name, str(default)))
        return value if math.isfinite(value) and value > 0 else default
    except ValueError:
        return default


def resource_settings():
    """Report effective limits without exposing unrelated host configuration."""
    return dict(max_temporal_labels=integer_setting('ROUTE_PLAN_MAX_TEMPORAL_LABELS', DEFAULT_MAX_TEMPORAL_LABELS),
                memory_reserve_mb=integer_setting('ROUTE_PLAN_MEMORY_RESERVE_MB', DEFAULT_MEMORY_RESERVE_MB),
                process_memory_limit_mb=integer_setting('ROUTE_PLAN_PROCESS_MEMORY_MB', DEFAULT_PROCESS_MEMORY_MB),
                max_search_subprocesses=2 * min(2, integer_setting('ROUTE_PLAN_DEPARTURE_CONCURRENCY', 2, minimum=1))
                    if integer_setting('ROUTE_PLAN_DIRECTION_PROCESSES', 2) == 2 else 0,
                timeout_seconds=integer_setting('ROUTE_PLAN_TIMEOUT_SECONDS', DEFAULT_TIMEOUT_SECONDS, minimum=1),
                grid_cache_mb=integer_setting('WIND_GRID_CACHE_MB', 64),
                decoded_grid_cache_mb=integer_setting('WIND_GRID_CACHE_MB', 64) * .75,
                encoded_frame_cache_mb=integer_setting('WIND_GRID_CACHE_MB', 64) * .25)


def available_memory_mb():
    """Available physical memory; None means the host cannot be queried."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                            *[(name, ctypes.c_ulonglong) for name in
                              ("total", "available", "page_total", "page_available",
                               "virtual_total", "virtual_available", "extended_available")]]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.available / 1024 ** 2
        elif sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
        else:
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024 ** 2
    except (OSError, ValueError, AttributeError):
        pass
    return None


def memory_pressure():
    reserve = integer_setting("ROUTE_PLAN_MEMORY_RESERVE_MB", DEFAULT_MEMORY_RESERVE_MB)
    available = available_memory_mb() if reserve else None
    if available is not None and available < reserve:
        return {"limit_type": "available_memory", "available_memory_mb": round(available, 1),
                "memory_reserve_mb": reserve}
    limit = integer_setting("ROUTE_PLAN_PROCESS_MEMORY_MB", DEFAULT_PROCESS_MEMORY_MB)
    current = process_memory_mb() if limit else None
    if current is not None and current >= limit:
        return {"limit_type": "process_memory", "process_memory_mb": round(current, 1),
                "process_memory_limit_mb": limit}
    return None


def process_memory_mb():
    """Current resident memory, rather than a past job's peak working set."""
    try:
        if sys.platform == "win32":
            return _windows_process_memory()[0]
        if sys.platform.startswith("linux"):
            resident = int(Path("/proc/self/statm").read_text().split()[1])
            return resident * os.sysconf("SC_PAGE_SIZE") / 1024 ** 2
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _windows_process_memory():
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                    *[(name, ctypes.c_size_t) for name in ("peak_working_set", "working_set", "peak_paged", "paged",
                                                          "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    current = ctypes.windll.kernel32.GetCurrentProcess
    current.restype = wintypes.HANDLE
    get_info = ctypes.windll.psapi.GetProcessMemoryInfo
    # windll caches function objects across threads. A per-call Counters class
    # cannot appear in shared argtypes: another thread may replace its pointer
    # class between assignment and invocation. The Win32 API takes a void*.
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    if get_info(current(), ctypes.byref(counters), counters.cb):
        return counters.working_set / 1024 ** 2, counters.peak_working_set / 1024 ** 2
    return None, None


def process_peak_memory_mb():
    """Process peak working set (includes runtime, cache and search), not labels alone."""
    try:
        if sys.platform == "win32":
            peak = _windows_process_memory()[1]
            return round(peak, 2) if peak is not None else None
        else:
            import resource
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return round(value / (1024 ** 2 if sys.platform == "darwin" else 1024), 2)
    except (OSError, ValueError, AttributeError, ImportError):
        pass
    return None
