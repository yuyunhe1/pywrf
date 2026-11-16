"""Native WRF holds and legacy/GFS sampling at UTC ten-minute boundaries."""
import math
from .wrf_native_schema import NATIVE_WRF_TIME_MODEL

WIND_TIME_STEP_SECONDS = 600
WIND_TIME_MODEL = "HOURLY_UV_TEN_MINUTE_HOLD_V1"


def frame_time(stamp):
    return math.floor(stamp / WIND_TIME_STEP_SECONDS) * WIND_TIME_STEP_SECONDS


def frame_boundaries(start, end):
    """Strictly interior frame boundaries; departure/arrival stay unrounded."""
    first = frame_time(start) + WIND_TIME_STEP_SECONDS
    while first < end:
        yield first
        first += WIND_TIME_STEP_SECONDS


def held_vectors(start, end, left, right, time_model=WIND_TIME_MODEL):
    """Yield [begin,end) constant vectors, using adjacent hours only.

    An exact known hourly frame is usable for its first ten minutes even if
    the next hour is missing. Other frames require both adjacent hourly U/Vs.
    No interval beyond the last supplied forecast is generated.
    """
    left_ok = all(map(math.isfinite, left))
    right_ok = all(map(math.isfinite, right))
    adjacent = time_model != NATIVE_WRF_TIME_MODEL and math.isclose(end - start, 3600, abs_tol=.001)
    cuts = [start, *frame_boundaries(start, end), end]
    for begin, finish in zip(cuts, cuts[1:]):
        sample = frame_time(begin)
        if abs(sample - start) < 1e-6 and left_ok:
            vector = left
        elif adjacent and left_ok and right_ok and start < sample < end:
            alpha = (sample - start) / (end - start)
            vector = tuple(a + alpha * (b - a) for a, b in zip(left, right))
        else:
            vector = None
        yield begin, finish, vector
