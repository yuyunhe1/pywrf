import numpy as np
import pytest

from app.models import Thresholds
from app.route_service import anchor_route_endpoints, haversine_km
from app.routing import plan_route
from app.wind_provider import WindGrid


def _grid() -> WindGrid:
    shape = (3, 4)
    return WindGrid(
        lons=np.asarray([118.0, 118.25, 118.5, 118.75]),
        lats=np.asarray([31.0, 30.75, 30.5]),
        u=np.ones(shape, dtype=float),
        v=np.zeros(shape, dtype=float),
        cycle="2026-07-20 00:00 UTC",
        forecast_hour=3,
        level="100m AGL",
        valid_time="2026-07-20 03:00 UTC",
    )


def _diagonal_grid_with_corner_wind(corner_wind_speed: float) -> WindGrid:
    u = np.ones((3, 3), dtype=float)
    u[0, 1] = corner_wind_speed
    return WindGrid(
        lons=np.asarray([0.0, 1.0, 2.0]),
        lats=np.asarray([2.0, 1.0, 0.0]),
        u=u,
        v=np.zeros((3, 3), dtype=float),
        cycle="2026-07-20 00:00 UTC",
        forecast_hour=3,
        level="100m AGL",
        valid_time="2026-07-20 03:00 UTC",
    )


def test_anchor_route_endpoints_replaces_grid_centres():
    start = (118.04, 30.96)
    end = (118.21, 30.94)

    points = anchor_route_endpoints([(118.0, 31.0), (118.25, 31.0)], start, end)

    assert points == [start, end]


def test_astar_directly_connects_real_points_in_adjacent_cells():
    start = (118.04, 30.96)
    end = (118.21, 30.94)

    result = plan_route(_grid(), start, end, Thresholds(danger=7.9))

    assert result["points"] == [start, end]
    assert result["distance_km"] == pytest.approx(haversine_km(start, end), abs=0.001)


@pytest.mark.parametrize("planner_type", ["lpa_star", "wa_lpa_star"])
def test_incremental_planners_directly_connect_real_points_in_adjacent_cells(planner_type):
    from app.main import plan_single_altitude_route

    start = (118.04, 30.96)
    end = (118.21, 30.94)
    result = plan_single_altitude_route(
        _grid(),
        start,
        end,
        Thresholds(danger=7.9),
        {"thresholds": {"max_wind_speed": 7.9}},
        planner_type,
    )

    assert result["points"] == [start, end]
    assert result["distance_km"] == pytest.approx(haversine_km(start, end), abs=0.001)


@pytest.mark.parametrize("planner_type", ["astar", "lpa_star", "wa_lpa_star"])
def test_diagonal_adjacent_cells_detour_around_blocked_corner(planner_type):
    from app.main import plan_single_altitude_route

    # The direct segment enters row=0/col=1 (9 m/s) before reaching the goal
    # cell.  The safe route must use row=1/col=0 instead.
    start = (0.4, 1.9)
    end = (1.0, 1.4)
    result = plan_single_altitude_route(
        _diagonal_grid_with_corner_wind(9.0),
        start,
        end,
        Thresholds(danger=7.9),
        {"thresholds": {"max_wind_speed": 7.9}},
        planner_type,
    )

    assert result["points"] == [start, (0.0, 1.0), end]
    assert result["distance_km"] > haversine_km(start, end)


@pytest.mark.parametrize("planner_type", ["astar", "lpa_star", "wa_lpa_star"])
def test_wind_avoidance_penalizes_flyable_risky_diagonal_corner(planner_type):
    from app.main import plan_single_altitude_route

    start = (0.4, 1.9)
    end = (1.0, 1.4)
    result = plan_single_altitude_route(
        _diagonal_grid_with_corner_wind(6.5),
        start,
        end,
        Thresholds(danger=7.9),
        {
            "weights": {"beta_wind": 250.0},
            "thresholds": {"max_wind_speed": 7.9},
        },
        planner_type,
    )

    assert result["points"] == [start, (0.0, 1.0), end]
