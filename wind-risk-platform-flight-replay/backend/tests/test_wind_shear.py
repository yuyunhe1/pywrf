import numpy as np
import pytest

from app.models import Thresholds
from app.cost_evaluator import evaluate_edge_flyability, evaluate_node_flyability
from app.routing import plan_route
from app.route_service import include_wind_shear_in_high_risk_ratio
from app.wind_provider import WindGrid
from app.wind_shear import (
    SHEAR_NO_FLY,
    SHEAR_SAFE,
    WindShearBlockedError,
    WindShearEnvironment,
    analyze_route_wind_shear,
    build_vertical_wind_shear_field,
    compute_direction_change,
    compute_horizontal_wind_shear,
    compute_vertical_wind_shear,
    ensure_wind_shear_config,
)


def _grid(u, v=None, level="100m AGL"):
    u = np.asarray(u, dtype=float)
    v = np.zeros_like(u) if v is None else np.asarray(v, dtype=float)
    rows, cols = u.shape
    return WindGrid(
        lons=np.arange(cols, dtype=float) * 0.03 + 118.0,
        lats=31.0 - np.arange(rows, dtype=float) * 0.03,
        u=u,
        v=v,
        cycle="2026-07-20 00:00 UTC",
        forecast_hour=3,
        level=level,
        valid_time="2026-07-20 03:00 UTC",
    )


def _horizontal_environment(**overrides):
    config = {
        "enabled": True,
        "horizontal": {
            "hard_delta_wind_vector_ms": 5.4,
            "hard_constraint_enabled": True,
            **overrides,
        },
    }
    return WindShearEnvironment(config=ensure_wind_shear_config(config))


def test_default_horizontal_thresholds_match_platform_controls():
    config = ensure_wind_shear_config()

    assert config.horizontal.hard_delta_wind_vector_ms == 5.4


def test_legacy_horizontal_threshold_name_is_still_accepted():
    config = ensure_wind_shear_config({"horizontal": {"hard_delta_v_1km_ms": 4.2}})

    assert config.horizontal.hard_delta_wind_vector_ms == 4.2


def test_uniform_vector_wind_has_zero_vertical_and_horizontal_shear():
    vertical = compute_vertical_wind_shear(3.0, 4.0, 80.0, 3.0, 4.0, 200.0)
    horizontal = compute_horizontal_wind_shear((118.0, 31.0), (118.03, 31.0), 3.0, 4.0, 3.0, 4.0)

    assert vertical["vertical_shear_s1"] == 0.0
    assert vertical["delta_v_10m_ms"] == 0.0
    assert vertical["shear_level"] == SHEAR_SAFE
    assert horizontal["horizontal_shear_s1"] == 0.0
    assert horizontal["delta_v_1km_ms"] == 0.0
    assert horizontal["shear_level"] == SHEAR_SAFE


def test_high_risk_ratio_combines_wind_and_wind_shear_evaluations():
    samples = [{"wind_speed": 2.0}, {"wind_speed": 3.0}]
    shear = {
        "enabled": True,
        "vertical_shear_evaluation_count": 2,
        "horizontal_shear_evaluation_count": 2,
        "vertical_shear_warning_count": 1,
        "horizontal_shear_warning_count": 1,
    }

    result = include_wind_shear_in_high_risk_ratio({}, samples, shear, Thresholds())

    assert result["wind_only_danger_ratio"] == 0.0
    assert result["wind_shear_risk_ratio"] == 0.5
    assert result["danger_ratio"] == 0.25
    assert result["high_risk_evaluation_count"] == 1
    assert result["risk_evaluation_count"] == 4


def test_equal_speed_opposite_direction_is_strong_vector_shear():
    result = compute_vertical_wind_shear(
        5.0,
        0.0,
        0.0,
        -5.0,
        0.0,
        10.0,
        {"vertical": {"enabled": True, "hard_constraint_enabled": True}},
    )

    assert result["delta_wind_vector_ms"] == 10.0
    assert result["direction_change_deg"] == pytest.approx(180.0)
    assert result["shear_level"] == SHEAR_NO_FLY
    assert result["is_flyable"] is False


def test_vertical_hard_threshold_at_three_metres_per_second_per_ten_metres():
    result = compute_vertical_wind_shear(
        0.0,
        1.0,
        100.0,
        3.0,
        1.0,
        110.0,
        {"vertical": {"enabled": True, "hard_constraint_enabled": True}},
    )

    assert result["delta_v_10m_ms"] == pytest.approx(3.0)
    assert result["is_flyable"] is False
    assert result["blocked_reason"] == "vertical_wind_shear"


def test_extreme_vertical_thirty_metre_threshold_blocks():
    result = compute_vertical_wind_shear(
        0.0,
        1.0,
        100.0,
        6.1,
        1.0,
        130.0,
        {"vertical": {"enabled": True, "hard_constraint_enabled": True}},
    )

    assert result["delta_v_30m_ms"] == pytest.approx(6.1)
    assert result["is_flyable"] is False


def test_horizontal_vector_change_threshold_uses_raw_vector_difference():
    latitude_delta = 1.0 / 111.195
    result = compute_horizontal_wind_shear(
        (118.0, 31.0),
        (118.0, 31.0 + latitude_delta),
        1.0,
        0.0,
        6.5,
        0.0,
    )

    assert result["delta_wind_vector_ms"] == pytest.approx(5.5)
    assert result["horizontal_distance_m"] == pytest.approx(1000.0, rel=0.002)
    assert result["horizontal_wind_shear_ms"] == pytest.approx(5.5)
    assert result["is_flyable"] is False
    assert result["blocked_reason"] == "horizontal_wind_shear"

    far_result = compute_horizontal_wind_shear(
        (118.0, 31.0),
        (119.0, 31.0),
        1.0,
        0.0,
        6.5,
        0.0,
    )
    assert far_result["horizontal_wind_shear_ms"] == result["horizontal_wind_shear_ms"]
    assert far_result["is_flyable"] is False


def test_horizontal_threshold_change_updates_edge_constraint():
    latitude_delta = 1.0 / 111.195
    common = ((118.0, 31.0), (118.0, 31.0 + latitude_delta), 1.0, 0.0, 6.5, 0.0)

    blocked = compute_horizontal_wind_shear(*common)
    allowed = compute_horizontal_wind_shear(
        *common,
        {"horizontal": {"hard_delta_wind_vector_ms": 6.0}},
    )

    assert blocked["is_flyable"] is False
    assert allowed["is_flyable"] is True


def test_direction_change_is_diagnostic_not_a_hard_constraint():
    result = compute_horizontal_wind_shear((118.0, 31.0), (119.0, 31.0), 1.0, 0.0, 0.0, 1.0)

    assert result["direction_change_deg"] == pytest.approx(90.0)
    assert result["delta_wind_vector_ms"] == pytest.approx(np.sqrt(2.0))
    assert result["is_flyable"] is True


def test_calm_wind_direction_change_does_not_trigger_constraint():
    result = compute_horizontal_wind_shear((118.0, 31.0), (119.0, 31.0), 0.1, 0.0, -0.1, 0.0)

    assert compute_direction_change(0.1, 0.0, -0.1, 0.0) is None
    assert result["direction_change_deg"] is None
    assert result["is_flyable"] is True


def test_uniform_route_profile_keeps_zero_shear_segments():
    grid = _grid([[3.0, 3.0, 3.0]])
    points = [(float(lon), float(grid.lats[0])) for lon in grid.lons]

    result = analyze_route_wind_shear(points, grid, _horizontal_environment())

    assert result["max_horizontal_wind_shear"] == 0.0
    assert result["max_horizontal_wind_shear_unit"] == "m/s"
    assert len(result["horizontal_wind_shear_profile"]) == 2
    assert [item["horizontal_wind_shear"] for item in result["horizontal_wind_shear_profile"]] == [0.0, 0.0]


def test_single_route_vector_change_has_one_event_at_real_distance():
    grid = _grid([[1.0, 1.0, 4.0]])
    points = [(float(lon), float(grid.lats[0])) for lon in grid.lons]

    result = analyze_route_wind_shear(points, grid, _horizontal_environment())
    profile = result["horizontal_wind_shear_profile"]

    assert len(profile) == 2
    assert profile[0]["horizontal_wind_shear"] == 0.0
    assert profile[1]["horizontal_wind_shear"] > 0.0
    assert profile[1]["center_distance_km"] == pytest.approx(
        (profile[1]["start_distance_km"] + profile[1]["end_distance_km"]) / 2.0,
        abs=0.001,
    )
    assert result["max_horizontal_wind_shear"] == profile[1]["horizontal_wind_shear"]
    assert result["max_horizontal_wind_shear_segment"]["segment_index"] == 1


def test_route_profile_uses_uv_vector_difference_not_scalar_speed_difference():
    grid = _grid([[3.0, 0.0]], [[4.0, 5.0]])
    points = [(float(lon), float(grid.lats[0])) for lon in grid.lons]

    result = analyze_route_wind_shear(points, grid, _horizontal_environment())
    item = result["horizontal_wind_shear_profile"][0]

    assert np.hypot(grid.u[0, 0], grid.v[0, 0]) == np.hypot(grid.u[0, 1], grid.v[0, 1])
    assert item["delta_wind_vector_ms"] == pytest.approx(np.sqrt(10.0), abs=1e-6)
    assert item["horizontal_wind_shear"] == item["delta_wind_vector_ms"]


def test_multiple_route_vector_changes_preserve_order_and_cumulative_mileage():
    grid = _grid([[0.0, 2.0, 2.0, 6.0, 6.0]])
    points = [(float(lon), float(grid.lats[0])) for lon in grid.lons]

    result = analyze_route_wind_shear(points, grid, _horizontal_environment())
    profile = result["horizontal_wind_shear_profile"]
    events = [item for item in profile if item["horizontal_wind_shear"] > 0.01]

    assert len(profile) == len(points) - 1
    assert [item["segment_index"] for item in events] == [0, 2]
    assert [item["center_distance_km"] for item in events] == sorted(
        item["center_distance_km"] for item in events
    )
    assert profile[1]["start_distance_km"] == profile[0]["end_distance_km"]
    assert profile[2]["start_distance_km"] == profile[1]["end_distance_km"]
    assert result["max_horizontal_wind_shear"] == max(
        item["horizontal_wind_shear"] for item in profile
    )


def test_vertical_field_uses_actual_layer_spacing_and_reports_missing_data():
    lower = _grid(np.ones((2, 2)), level="80m AGL")
    upper = _grid(np.ones((2, 2)) * 4.0, level="200m AGL")
    field = build_vertical_wind_shear_field([lower, upper], 100.0)
    missing = build_vertical_wind_shear_field([lower], 100.0)

    assert field.delta_v_10m_ms[0, 0] == pytest.approx(3.0 / 120.0 * 10.0)
    assert field.lower_height_m == 80.0
    assert field.upper_height_m == 200.0
    assert missing.status == "missing"
    assert missing.vertical_shear_s1 is None


def test_vertical_shear_does_not_block_node():
    selected = _grid(np.ones((3, 3)), level="100m AGL")
    lower = _grid(np.ones((3, 3)), level="80m AGL")
    upper_wind = np.ones((3, 3))
    upper_wind[1, 1] = 37.0  # 36 m/s / 120 m = 3 m/s per 10 m.
    upper = _grid(upper_wind, level="200m AGL")
    config = ensure_wind_shear_config()
    environment = WindShearEnvironment(
        config=config,
        vertical=build_vertical_wind_shear_field([lower, upper], 100.0, config),
    )

    result = evaluate_node_flyability(
        (1, 1),
        selected,
        None,
        None,
        {"thresholds": {"max_wind_speed": 7.9}},
        environment,
    )

    assert result["is_flyable"] is True
    assert result["blocked_reason"] is None


def test_horizontal_shear_blocks_only_the_corresponding_edge_not_its_nodes():
    grid = _grid([[1.0, -1.0, -1.0]])
    environment = _horizontal_environment(hard_delta_wind_vector_ms=0.5)
    cost_config = {"thresholds": {"max_wind_speed": 7.9}}

    left_node = evaluate_node_flyability((0, 0), grid, None, None, cost_config, environment)
    middle_node = evaluate_node_flyability((0, 1), grid, None, None, cost_config, environment)
    blocked_edge = evaluate_edge_flyability((0, 0), (0, 1), grid, None, None, cost_config, environment)
    safe_edge = evaluate_edge_flyability((0, 1), (0, 2), grid, None, None, cost_config, environment)

    assert left_node["is_flyable"] is True
    assert middle_node["is_flyable"] is True
    assert blocked_edge["is_flyable"] is False
    assert blocked_edge["blocked_reason"] == "horizontal_wind_shear"
    assert bool(safe_edge["is_flyable"]) is True


def test_diagonal_edge_cannot_bypass_shear_on_orthogonal_corner_edges():
    grid = _grid([[0.0, 6.0], [6.0, 0.0]])
    environment = _horizontal_environment(hard_delta_wind_vector_ms=5.4)
    cost_config = {"thresholds": {"max_wind_speed": 7.9}}

    direct_shear = compute_horizontal_wind_shear(
        (grid.lons[0], grid.lats[0]),
        (grid.lons[1], grid.lats[1]),
        grid.u[0, 0],
        grid.v[0, 0],
        grid.u[1, 1],
        grid.v[1, 1],
        environment.config,
    )
    diagonal_edge = evaluate_edge_flyability(
        (0, 0),
        (1, 1),
        grid,
        None,
        None,
        cost_config,
        environment,
    )

    assert direct_shear["delta_wind_vector_ms"] == 0.0
    assert direct_shear["is_flyable"] is True
    assert diagonal_edge["is_flyable"] is False
    assert diagonal_edge["blocked_reason"] == "horizontal_wind_shear"
    assert diagonal_edge["edge_cost"].horizontal_delta_wind_vector_ms == pytest.approx(6.0)


def test_astar_cannot_escape_shear_enclosed_start_through_diagonal():
    grid = _grid([
        [0.0, 6.0, 0.0],
        [6.0, 0.0, 6.0],
        [0.0, 6.0, 0.0],
    ])

    with pytest.raises(WindShearBlockedError) as exc_info:
        plan_route(
            grid,
            (grid.lons[1], grid.lats[1]),
            (grid.lons[0], grid.lats[0]),
            Thresholds(danger=7.9),
            cost_config={"weights": {"beta_wind": 0.0, "gamma_headwind": 0.0, "delta_crosswind": 0.0}},
            wind_shear_environment=_horizontal_environment(hard_delta_wind_vector_ms=5.4),
        )

    assert exc_info.value.blocked_count >= 8


def test_route_shear_analysis_skips_cross_altitude_transitions():
    grid = _grid([[1.0, -1.0, 1.0]])
    environment = _horizontal_environment(hard_delta_wind_vector_ms=0.5)
    points = [
        {"lon": grid.lons[0], "lat": grid.lats[0], "altitude_agl_m": 100.0},
        {"lon": grid.lons[1], "lat": grid.lats[0], "altitude_agl_m": 200.0},
        {"lon": grid.lons[2], "lat": grid.lats[0], "altitude_agl_m": 200.0},
    ]

    result = analyze_route_wind_shear(points, grid, environment)

    assert result["vertical_status"] == "disabled"
    assert result["vertical_shear_evaluation_count"] == 0
    assert result["horizontal_shear_evaluation_count"] == 1
    assert result["horizontal_shear_warning_count"] == 1


@pytest.mark.parametrize("planner_type", ["astar", "lpa_star", "wa_lpa_star"])
def test_grid_planners_detour_around_horizontal_shear_barrier(planner_type):
    from app.main import plan_single_altitude_route

    u = np.ones((5, 5), dtype=float) * 5.0
    u[2, 2] = -5.0
    grid = _grid(u)
    result = plan_single_altitude_route(
        grid,
        (grid.lons[0], grid.lats[2]),
        (grid.lons[-1], grid.lats[2]),
        Thresholds(danger=7.9),
        {"weights": {"beta_wind": 0.0, "gamma_headwind": 0.0, "delta_crosswind": 0.0}},
        planner_type,
        _horizontal_environment(hard_delta_wind_vector_ms=2.0),
    )

    assert (float(grid.lons[2]), float(grid.lats[2])) not in result["points"]
    assert len(result["points"]) > 2


def test_astar_reports_wind_shear_blocked_when_barrier_closes_domain():
    u = np.ones((5, 5), dtype=float) * 5.0
    u[:, 2] = -5.0
    grid = _grid(u)

    with pytest.raises(WindShearBlockedError) as exc_info:
        plan_route(
            grid,
            (grid.lons[0], grid.lats[2]),
            (grid.lons[-1], grid.lats[2]),
            Thresholds(danger=7.9),
            cost_config={"weights": {"beta_wind": 0.0, "gamma_headwind": 0.0, "delta_crosswind": 0.0}},
            wind_shear_environment=_horizontal_environment(hard_delta_wind_vector_ms=2.0),
        )

    assert exc_info.value.blocked_count > 0
    assert "风切变" in str(exc_info.value)


def test_route_api_returns_reference_path_when_shear_fully_blocks(monkeypatch):
    from app import main
    from app.models import RoutePlanRequest

    u = np.ones((5, 5), dtype=float) * 5.0
    u[:, 2] = -5.0
    grid = _grid(u)
    environment = _horizontal_environment(hard_delta_wind_vector_ms=2.0)
    monkeypatch.setenv("ROUTE_PLAN_MULTI_ALTITUDE", "0")
    monkeypatch.setattr(main, "load_grid", lambda *args, **kwargs: grid)
    monkeypatch.setattr(main, "build_route_wind_shear_environment", lambda *args, **kwargs: environment)

    result = main.route_plan(
        RoutePlanRequest(
            start=(float(grid.lons[0]), float(grid.lats[2])),
            end=(float(grid.lons[-1]), float(grid.lats[2])),
            cycle=grid.cycle,
            forecast_hour=grid.forecast_hour,
            level=grid.level,
            planner_type="astar",
            planning_strategy="distance_priority",
        )
    )

    assert len(result["points"]) >= 2
    assert result["wind_shear_fallback"]["active"] is True
    assert result["wind_shear_fallback"]["reason"] == "wind_shear_blocked"
    assert result["analysis"]["wind_shear_fallback"] == result["wind_shear_fallback"]
    assert result["analysis"]["wind_shear"]["highest_shear_level"] == SHEAR_NO_FLY
    assert result["analysis"]["danger_ratio"] > result["analysis"]["wind_only_danger_ratio"]
    assert result["max_horizontal_wind_shear"] is None
    assert result["horizontal_wind_shear_profile"] == []
    assert result["wind_shear"]["final_route_available"] is False
    assert result["analysis"]["navigation_decision"] == "禁止通航"
    assert result["analysis"]["horizontal_wind_shear_risk"] is True


def test_route_plan_skips_graph_search_when_endpoint_wind_is_hard_blocked(monkeypatch):
    from app import main
    from app.models import RoutePlanRequest

    grid = _grid(np.ones((3, 3), dtype=float))
    grid.u[1, 1] = 8.2

    monkeypatch.setattr(main, "load_grid", lambda *args, **kwargs: grid)

    def fail_if_search_preparation_runs(*args, **kwargs):
        raise AssertionError("route search preparation should be skipped")

    monkeypatch.setattr(main, "thin_route_planning_grid", fail_if_search_preparation_runs)
    result = main.route_plan(
        RoutePlanRequest(
            start=(float(grid.lons[1]), float(grid.lats[1])),
            end=(float(grid.lons[2]), float(grid.lats[2])),
            cycle=grid.cycle,
            forecast_hour=grid.forecast_hour,
            level=grid.level,
            planner_type="wa_lpa_star",
            planning_strategy="wind_avoidance",
        )
    )

    assert result["points"] == []
    assert result["planning_skipped"] is True
    assert result["planning_skip_reason"] == "endpoint_wind_speed"
    assert result["analysis"]["navigation_decision"] == "禁止通航"
    assert result["analysis"]["max_wind_speed"] == pytest.approx(8.2)
    assert "已跳过航线图搜索" in result["analysis"]["navigation_decision_reason"]
