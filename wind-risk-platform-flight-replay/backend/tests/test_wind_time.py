from datetime import timedelta
import math

import numpy as np
import pytest

from app import wrf_timeline as timeline
from app.departure_windows import _vector_issues, prepare_evaluation, describe_evaluation
from app.wind_time import frame_time, frame_boundaries, WIND_TIME_STEP_SECONDS
from app.temporal_routing import TemporalWeather
from app.route_options import build_strategy_cost_config
from app.models import WindShearSettings
from test_flight_replay import dataset, BASE, profile, ROUTE
from test_temporal_routing import request


def test_hourly_vectors_are_sampled_at_ten_minute_boundaries_and_held():
    data = dataset([(0, 2), (1, 8), (2, 2)])
    for minutes, expected in [(0, 2), (9.999, 2), (10, 3), (19.999, 3), (30, 5), (59.999, 7), (60, 8)]:
        stamp = BASE.timestamp() + minutes * 60
        assert timeline.vector_at(data, stamp, .1, 0) == pytest.approx((expected, 0))
        frame = timeline.wind_frame(data, stamp)
        assert frame["metadata"]["wind_time_step_seconds"] == 600
        assert frame["metadata"]["valid_time"] == timeline.iso(frame_time(stamp))
        assert frame["velocity"][0]["data"][0] == pytest.approx(expected)


def test_held_uv_can_reverse_and_reach_calm_without_interpolating_scalar_speed():
    data = dataset([(0, 8), (1, -8), (2, 8)])
    assert timeline.vector_at(data, BASE.timestamp() + 35 * 60, .1, 0) == (0., 0.)
    assert timeline.vector_at(data, BASE.timestamp() + 40 * 60, .1, 0)[0] < 0


def test_danger_starts_on_frame_boundary_and_recovers_without_an_extra_blocked_instant():
    times = [BASE.timestamp() + h * 3600 for h in range(3)]
    missing, unsafe = _vector_issues(times, [(0, 0), (12, 0), (0, 0)], 8)
    assert not missing and len(unsafe) == 1
    assert not unsafe[0].contains(times[0] + 40 * 60 - .001)
    assert unsafe[0].contains(times[0] + 40 * 60)
    assert unsafe[0].contains(times[0] + 90 * 60 - .001)
    assert not unsafe[0].contains(times[0] + 90 * 60)


def test_missing_hour_cannot_supply_intermediate_frames_or_extend_forecast_end():
    data = dataset([(0, 2), (2, 2), (3, 2)])
    start = BASE.timestamp()
    assert timeline.vector_at(data, start + 599.999, .1, 0) == (2., 0.)
    assert timeline.vector_at(data, start + 600, .1, 0) is None
    assert timeline.vector_at(data, start + 3 * 3600 + .001, .1, 0) is None
    data = dataset([(0, 2), (1, np.nan), (2, 2)])
    assert timeline.vector_at(data, start + 599, .1, 0) == (2., 0.)
    assert timeline.vector_at(data, start + 600, .1, 0) is None


def test_profile_samples_match_frame_queries_and_keep_exact_eta():
    data = dataset([(0, 1), (1, 10), (2, 1), (3, 1)])
    result = profile(data, minutes=3)
    assert result["wind_time_step_seconds"] == 600
    for sample in result["analysis"]["samples"]:
        assert sample["elapsed_seconds"] == pytest.approx(sample["distance_km"] * 100)
        at = BASE.timestamp() + 180 + sample["elapsed_seconds"]
        expected = timeline.vector_at(data, at, sample["lon"], sample["lat"])
        assert sample["wind_speed"] == pytest.approx(math.hypot(*expected))
    switches = {segment["start_seconds"] for segment in result["segments"]}
    assert 420 in switches and 1020 in switches


def test_search_and_window_hazards_use_identical_held_wind_and_shear():
    data = dataset([(0, [1, 1, 1, 1, 1]), (1, [9, -9, 9, -9, 9]), (2, 1), (3, 1)])
    req = request().model_copy(update={"departure_at": BASE, "start": ROUTE["points"][0], "end": ROUTE["points"][-1],
                                      "wind_shear": WindShearSettings()})
    weather = TemporalWeather(data, req, build_strategy_cost_config("wind_avoidance"))
    for a, b in [ROUTE["points"], ((.01, .05), (.02, .05))]:
        edge = weather.edge(a, b)
        check = prepare_evaluation([a, b], list(data.frames), 10, 7.9, 5.4)
        for minute in [0, 9.999, 10, 39.999, 40, 59.999, 60, 69.999, 70, 89.999, 90]:
            now = BASE + timedelta(minutes=minute)
            assert edge.allowed(now.timestamp()) == (describe_evaluation(check, now, 10)["current_departure"]["status"] == "allowed")


def test_clock_boundaries_do_not_round_departure_to_nearest_frame():
    stamp = BASE.timestamp() + 3 * 60
    assert list(frame_boundaries(stamp, stamp + 1800)) == [BASE.timestamp() + n * 600 for n in (1, 2, 3)]
    assert WIND_TIME_STEP_SECONDS == 600
