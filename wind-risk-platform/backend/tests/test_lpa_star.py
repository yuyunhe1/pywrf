import numpy as np


def _grid():
    from app.wind_provider import WindGrid

    lons = np.arange(9, dtype=float)
    lats = np.arange(8, -1, -1, dtype=float)
    return WindGrid(
        lons=lons,
        lats=lats,
        u=np.ones((9, 9), dtype=float),
        v=np.zeros((9, 9), dtype=float),
        cycle="2026-06-15 00:00 UTC",
        forecast_hour=3,
        level="100m AGL",
        valid_time="2026-06-15 03:00 UTC",
    )


def test_lpa_star_repairs_after_local_wind_change():
    from app.grid_planner_lpa_star import LPAStarPlanner
    from app.models import Thresholds

    grid = _grid()
    config = {"weights": {"beta_wind": 80.0, "gamma_headwind": 0.0, "delta_crosswind": 0.0}}
    planner = LPAStarPlanner(grid, {"row": 4, "col": 0}, {"row": 4, "col": 8}, cost_config=config, thresholds=Thresholds())
    initial = planner.plan()

    changed_nodes = []
    for col in range(2, 7):
        grid.u[4, col] = 7.6
        changed_nodes.append({"row": 4, "col": col})

    planner.update_environment(changed_nodes=changed_nodes)
    repaired = planner.plan()

    assert initial.points != repaired.points
    assert repaired.expanded_nodes < grid.u.size
    assert repaired.max_wind_speed < 7.9
