import math

import pytest

from app import planning_limits as limits, temporal_routing
from app.temporal_routing import TemporalSearchExhausted
from test_temporal_routing import dataset, planner, profile, request


def test_memory_reserve_uses_available_physical_memory(monkeypatch):
    monkeypatch.setenv("ROUTE_PLAN_MEMORY_RESERVE_MB", "1024")
    monkeypatch.setattr(limits, "available_memory_mb", lambda: 800.25)
    assert limits.memory_pressure() == {"limit_type": "available_memory",
                                       "available_memory_mb": 800.2, "memory_reserve_mb": 1024}
    monkeypatch.setattr(limits, "available_memory_mb", lambda: 2048.)
    assert limits.memory_pressure() is None
    monkeypatch.setattr(limits, "available_memory_mb", lambda: None)
    assert limits.memory_pressure() is None


def test_available_memory_probe_returns_positive_measurement_on_supported_host():
    value = limits.available_memory_mb()
    assert value is None or math.isfinite(value) and value > 0


def test_moderate_defaults_restore_a_finite_but_larger_label_budget(monkeypatch):
    monkeypatch.delenv('ROUTE_PLAN_MAX_TEMPORAL_LABELS', raising=False)
    assert limits.integer_setting('ROUTE_PLAN_MAX_TEMPORAL_LABELS', limits.DEFAULT_MAX_TEMPORAL_LABELS) == 600000
    assert limits.DEFAULT_MAX_TEMPORAL_LABELS > 180000
    assert limits.DEFAULT_TIMEOUT_SECONDS == 7200
    assert limits.DEFAULT_MAX_GRID_CELLS == 150000


def test_process_memory_budget_uses_current_not_historical_peak(monkeypatch):
    monkeypatch.delenv('ROUTE_PLAN_MEMORY_RESERVE_MB', raising=False)
    monkeypatch.delenv('ROUTE_PLAN_PROCESS_MEMORY_MB', raising=False)
    monkeypatch.setattr(limits, 'available_memory_mb', lambda: 5000.)
    monkeypatch.setattr(limits, 'process_memory_mb', lambda: 2048.)
    assert limits.memory_pressure() == {'limit_type': 'process_memory', 'process_memory_mb': 2048.,
                                        'process_memory_limit_mb': 2048}
    monkeypatch.setattr(limits, 'process_memory_mb', lambda: 500.)
    assert limits.memory_pressure() is None
    monkeypatch.setattr(limits, 'available_memory_mb', lambda: 1800.)
    assert limits.memory_pressure()['limit_type'] == 'available_memory'


def test_process_memory_probe_is_finite_when_supported():
    value = limits.process_memory_mb()
    assert value is None or math.isfinite(value) and value > 0


def test_concurrent_memory_probes_do_not_replace_each_others_ctypes_pointer_type():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda _: limits.process_memory_mb(), range(100)))
    assert all(value is None or math.isfinite(value) and value > 0 for value in values)


@pytest.mark.parametrize("algorithm", ["astar", "lpa_star", "wa_lpa_star"])
def test_zero_removes_label_count_limit_and_preserves_future_hazard_checks(monkeypatch, algorithm):
    def wind(hour, u, v):
        if hour >= 1:
            u[2, 3] = 12
    data = dataset(wind)
    monkeypatch.setenv("ROUTE_PLAN_MAX_TEMPORAL_LABELS", "1")
    with pytest.raises(TemporalSearchExhausted) as limited:
        planner(data).search(algorithm)
    assert limited.value.diagnostics["limit_type"] == "arrival_labels"
    monkeypatch.setenv("ROUTE_PLAN_MAX_TEMPORAL_LABELS", "0")
    result = planner(data).search(algorithm)
    assert result["arrival_labels"] > 1
    assert profile(data, result)["decision"]["status"] == "allowed"
    assert any(abs(p[1]) > .09 for p in result["points"])


def test_zero_grid_budget_keeps_original_grid_instead_of_coarsening_to_one_cell():
    search = planner(dataset(), max_grid_cells=0)
    assert search.stride == 1
    assert len(search.rows) == 5 and len(search.cols) == 7


def test_unlimited_labels_still_stop_before_memory_exhaustion(monkeypatch):
    def wall(hour, u, v):
        u[:, 3] = 12
    monkeypatch.setenv("ROUTE_PLAN_MAX_TEMPORAL_LABELS", "0")
    monkeypatch.setattr(temporal_routing, "memory_pressure", lambda: {
        "limit_type": "available_memory", "available_memory_mb": 900, "memory_reserve_mb": 1024})
    with pytest.raises(TemporalSearchExhausted, match="内存") as failure:
        planner(dataset(wall)).search()
    assert failure.value.diagnostics["limit_type"] == "available_memory"
    assert failure.value.diagnostics["expanded_nodes"] == 512


def test_memory_limit_keeps_already_validated_candidate(monkeypatch):
    from app import temporal_plan_service as service
    from app.segmented_routing import SegmentedPlanner
    def soft_wind(hour, u, v):
        u[2, 2:5] = 6
    original = SegmentedPlanner.expand_forward
    def constrained(self):
        if self.best is not None:
            raise TemporalSearchExhausted("搜索达到可用内存资源上限", {"limit_type": "available_memory"})
        return original(self)
    monkeypatch.setattr(SegmentedPlanner, "expand_forward", constrained)
    publications = []
    result = service.compute_temporal_plan(request(planning_strategy="wind_avoidance"), dataset(soft_wind),
                                           [-.1, -.3, .7, .3], 0, publications.append)
    assert publications and result["analysis"]["navigation_allowed"]
    assert not result["reference_route"]
    assert result["planning_diagnostics"]["attempts"][-1]["limit_type"] == "available_memory"


def test_full_domain_search_can_leave_the_original_endpoint_corridor(monkeypatch):
    from app import temporal_plan_service as service
    from app.segmented_routing import SegmentedPlanner
    original = SegmentedPlanner.__init__
    seen = []
    def capture(self, weather, start, end, bbox, max_grid_cells):
        seen.append(bbox)
        return original(self, weather, start, end, bbox, max_grid_cells)
    monkeypatch.setattr(SegmentedPlanner, "__init__", capture)
    monkeypatch.setenv("ROUTE_PLAN_SEARCH_FULL_DOMAIN", "1")
    service.compute_temporal_plan(request(), dataset(), [.0, -.01, .6, .01], 0)
    assert seen[0] == [0., -.2, .6, .2]
    monkeypatch.setenv("ROUTE_PLAN_SEARCH_FULL_DOMAIN", "0")
    service.compute_temporal_plan(request(), dataset(), [.0, -.01, .6, .01], 0)
    assert seen[-1] == [.0, -.01, .6, .01]


@pytest.mark.parametrize("bad", ["invalid", "nan", "inf", "0", "-5"])
def test_invalid_stage_budget_cannot_disable_timeout(monkeypatch, bad):
    monkeypatch.setenv("ROUTE_PLAN_FAST_SEARCH_TIMEOUT_SECONDS", bad)
    assert limits.seconds_setting("ROUTE_PLAN_FAST_SEARCH_TIMEOUT_SECONDS", 120) == 120


def test_synchronous_endpoint_uses_job_budget_and_retains_verified_route_on_timeout(monkeypatch):
    from app import main
    from app.planning_control import PlanningTimedOut, remaining_seconds
    from test_route_jobs import candidate
    monkeypatch.setattr(main.route_jobs, "timeout_seconds", 7200)
    def compute(req, publish):
        assert 7190 < remaining_seconds() <= 7200
        publish(candidate())
        raise PlanningTimedOut()
    monkeypatch.setattr(main, "compute_route_plan", compute)
    result = main.route_plan(request())
    assert result["planning_status"] == "timed_out"
    assert result["analysis"]["navigation_allowed"] and len(result["points"]) == 2


def test_v3_worker_receives_extended_plan_budget_instead_of_old_ten_minute_cap(monkeypatch):
    from app import v3_service
    received = {}
    def worker(operation, body, data, publish=None, **kwargs):
        received.update(operation=operation, **kwargs)
        return {"ok": True}
    monkeypatch.setenv("ROUTE_PLAN_TIMEOUT_SECONDS", "7200")
    monkeypatch.setattr(v3_service, "run_worker", worker)
    assert v3_service.compute_plan(request(), dataset={}) == {"ok": True}
    assert received == {"operation": "plan", "budget_seconds": 7200}
