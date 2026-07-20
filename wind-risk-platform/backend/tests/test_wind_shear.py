import numpy as np
import pytest

from app.models import Thresholds
from app.cost_evaluator import evaluate_node_flyability
from app.routing import plan_route
from app.route_service import include_wind_shear_in_high_risk_ratio
from app.wind_provider import WindGrid
from app.wind_shear import (
    SHEAR_NO_FLY,
    SHEAR_SAFE,
    WindShearBlockedError,
    WindShearEnvironment,
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
            "hard_delta_v_1km_ms": 2.6,
            "hard_direction_change_deg": 45.0,
            "hard_constraint_enabled": True,
            **overrides,
        },
    }
    return WindShearEnvironment(config=ensure_wind_shear_config(config))


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
    assert result["danger_ratio"] == 0.333
    assert result["high_risk_evaluation_count"] == 2
    assert result["risk_evaluation_count"] == 6


def test_equal_speed_opposite_direction_is_strong_vector_shear():
    result = compute_vertical_wind_shear(5.0, 0.0, 0.0, -5.0, 0.0, 10.0)

    assert result["delta_wind_vector_ms"] == 10.0
    assert result["direction_change_deg"] == pytest.approx(180.0)
    assert result["shear_level"] == SHEAR_NO_FLY
    assert result["is_flyable"] is False


def test_vertical_hard_threshold_at_three_metres_per_second_per_ten_metres():
    result = compute_vertical_wind_shear(0.0, 1.0, 100.0, 3.0, 1.0, 110.0)

    assert result["delta_v_10m_ms"] == pytest.approx(3.0)
    assert result["is_flyable"] is False
    assert result["blocked_reason"] == "vertical_wind_shear"


def test_extreme_vertical_thirty_metre_threshold_blocks():
    result = compute_vertical_wind_shear(0.0, 1.0, 100.0, 6.1, 1.0, 130.0)

    assert result["delta_v_30m_ms"] == pytest.approx(6.1)
    assert result["is_flyable"] is False


def test_horizontal_vector_change_threshold_blocks_real_distance_edge():
    # About 1 km north-south; 2.7 m/s vector change therefore exceeds 2.6 m/s/km.
    latitude_delta = 1.0 / 111.195
    result = compute_horizontal_wind_shear((118.0, 31.0), (118.0, 31.0 + latitude_delta), 1.0, 0.0, 3.7, 0.0)

    assert result["horizontal_distance_m"] == pytest.approx(1000.0, rel=0.002)
    assert result["delta_v_1km_ms"] >= 2.6
    assert result["is_flyable"] is False
    assert result["blocked_reason"] == "horizontal_wind_shear"


def test_direction_change_at_forty_five_degrees_blocks_horizontal_edge():
    result = compute_horizontal_wind_shear((118.0, 31.0), (119.0, 31.0), 1.0, 0.0, 1.0, 1.0)

    assert result["direction_change_deg"] == pytest.approx(45.0)
    assert result["is_flyable"] is False


def test_calm_wind_direction_change_does_not_trigger_constraint():
    result = compute_horizontal_wind_shear((118.0, 31.0), (119.0, 31.0), 0.1, 0.0, -0.1, 0.0)

    assert compute_direction_change(0.1, 0.0, -0.1, 0.0) is None
    assert result["direction_change_deg"] is None
    assert result["is_flyable"] is True


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


def test_vertical_shear_field_blocks_node_with_explicit_reason():
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

    assert result["is_flyable"] is False
    assert result["blocked_reason"] == "vertical_wind_shear"


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
        _horizontal_environment(),
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
            wind_shear_environment=_horizontal_environment(),
        )

    assert exc_info.value.blocked_count > 0
    assert "风切变" in str(exc_info.value)


def test_route_api_returns_reference_path_when_shear_fully_blocks(monkeypatch):
    from app import main
    from app.models import RoutePlanRequest

    u = np.ones((5, 5), dtype=float) * 5.0
    u[:, 2] = -5.0
    grid = _grid(u)
    environment = _horizontal_environment()
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
