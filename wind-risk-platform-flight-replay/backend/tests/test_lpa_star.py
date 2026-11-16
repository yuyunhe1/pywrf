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


def test_directed_edge_cache_invalidates_on_in_place_environment_update(monkeypatch):
    from app import grid_planner_lpa_star as module
    from app.models import Thresholds
    from app.grid_planner_wa_lpa_star import WALPAStarPlanner
    calls = []
    original = module.calculate_edge_cost
    def evaluate(*args, **kwargs):
        calls.append(args[:2])
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'calculate_edge_cost', evaluate)
    grid = _grid()
    planner = WALPAStarPlanner(grid, (4, 0), (4, 8), thresholds=Thresholds())
    first = planner.edge_cost((4, 0), (4, 1))
    assert planner.edge_cost((4, 0), (4, 1)) == first
    assert len(calls) == 1
    planner.edge_cost((4, 1), (4, 0))
    assert len(calls) == 2  # Wind makes edges directional.
    grid.u[4, 1] = 9.
    planner.update_environment(changed_nodes=[(4, 1)])
    assert np.isinf(planner.edge_cost((4, 0), (4, 1)))
    grid.u[4, 1] = 1.
    planner.reset_search()
    assert np.isfinite(planner.edge_cost((4, 0), (4, 1)))
