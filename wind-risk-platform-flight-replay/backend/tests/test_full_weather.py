"""Whole forecast loading must preserve hazards, geometry and temporal identity."""
from dataclasses import replace
from threading import Event
from time import monotonic

import numpy as np
import pytest

from app import local_weather
from app.local_weather import LocalWeather
from app.planning_control import planning_scope, PlanningCancelled
from app.route_options import build_strategy_cost_config
from app.segmented_routing import SegmentedPlanner
from app.wrf_timeline import load_dataset
from test_forecast_store import source_files
from test_temporal_routing import BASE, dataset, request, profile


def weather(data, req=None, mode="full"):
    req = req or request()
    return LocalWeather(data, req, build_strategy_cost_config(req.planning_strategy), loading_mode=mode)


def test_all_pinned_hours_and_far_native_cells_are_loaded_without_modifying_sources(source_files):
    data = load_dataset("100m AGL", BASE)
    original = data.frames[-1][1].u
    assert data.io_stats.snapshot()["full_decodes"] == 0
    full = weather(data)
    stats = full.diagnostics()
    assert stats["full_field_hours_loaded"] == len(data.frames) == 4
    assert stats["full_field_spatial_cells"] == 4 * 21 * 25
    assert stats["full_field_vector_values"] == 2 * 4 * 21 * 25
    assert data.io_stats.snapshot()["full_decodes"] == 4
    assert full.vector(data.frames[-1][0], 20, 24) == (2., 0.)
    assert full.stats["local_block_reads"] == 0
    assert data.frames[-1][1].u is original  # replay/transport keep immutable descriptors
    assert not full.full_fields[data.frames[-1][1].valid_time][0].flags.writeable


@pytest.mark.parametrize("source", ["wrf", "gfs"])
def test_full_and_local_agree_on_held_vectors_missing_data_and_complete_route_checks(source):
    def wind(hour, u, v):
        u[:] = hour + 1
        v[0, -1] = np.nan
        u[2, 3] = 12 if hour == 1 else 1
    data = replace(dataset(wind), source=source)
    req = request().model_copy(update={"source": source, "average_ground_speed_mps": 50 if source == "gfs" else 10})
    full, local = weather(data, req), weather(data, req, "local")
    for offset in (0, 420, 600, 3599, 3600, 5400, 15000):
        stamp = BASE.timestamp() + offset
        for row, col in ((0, 6), (2, 3), (4, 0)):
            assert full.vector(stamp, row, col) == local.vector(stamp, row, col)
        for a, b in (((0, 0), (.6, 0)), ((0, .2), (.6, .2))):
            fe, le = full.edge(a, b), local.edge(a, b)
            assert fe.allowed(stamp) == le.allowed(stamp)
            assert full.cost(fe, stamp) == local.cost(le, stamp)
    assert full.route_cost([req.start, req.end]) == local.route_cost([req.start, req.end])


@pytest.mark.parametrize("algorithm", ["astar", "lpa_star", "wa_lpa_star"])
def test_both_loading_modes_find_a_detour_beyond_one_ten_minute_loading_radius(algorithm):
    def wind(hour, u, v):
        u[1:4, 3] = 12
    data = dataset(wind)
    req = request(planner_type=algorithm).model_copy(update={"start": (.2, 0.), "end": (.4, 0.)})
    for mode in ("local", "full"):
        search = SegmentedPlanner(weather(data, req, mode), req.start, req.end, [0, -.2, .6, .2])
        with planning_scope(monotonic() + 15, Event()):
            result = search.search(algorithm)
        assert profile(data, result, req)["decision"]["status"] == "allowed"
        assert any(abs(point[1]) >= .19 for point in result["points"])
        assert result["search_domain_grid_cells"] == 35
        assert result["weather"]["weather_loading_mode"] == mode


def test_full_load_can_be_cancelled_and_does_not_fall_back_to_local_on_memory_pressure(monkeypatch):
    cancelled = Event()
    cancelled.set()
    with pytest.raises(PlanningCancelled):
        with planning_scope(monotonic()+10, cancelled):
            weather(dataset())
    monkeypatch.setattr(local_weather, "memory_pressure", lambda: {"available_memory_mb": 1})
    with pytest.raises(MemoryError, match="完整风场加载"):
        weather(dataset())
