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


def test_wa_lpa_star_has_wind_aware_heuristic():
    from app.grid_planner_wa_lpa_star import WALPAStarPlanner
    from app.models import Thresholds

    grid = _grid()
    planner = WALPAStarPlanner(
        grid,
        {"row": 4, "col": 0},
        {"row": 4, "col": 8},
        thresholds=Thresholds(),
        wa_config={"k_wind_align": 0.2},
    )

    tailwind_node = (4, 2)
    tailwind_h = planner.heuristic(tailwind_node)
    grid.u[tailwind_node] = -1.0
    headwind_h = planner.heuristic(tailwind_node)

    assert planner.wind_alignment_factor(tailwind_node) < 0
    assert headwind_h > tailwind_h


def test_should_replan_detects_path_wind_change():
    from app.grid_planner_wa_lpa_star import should_replan
    from app.models import Thresholds

    old_grid = _grid()
    new_grid = _grid()
    new_grid.u[4, 4] = 7.6
    path = [{"row": 4, "col": col} for col in range(9)]

    decision = should_replan(
        old_grid,
        new_grid,
        path,
        {"wind_change_threshold": 1.0},
        cost_config={"thresholds": {"max_wind_speed": Thresholds().danger}},
    )

    assert decision.replan_type == "local"
    assert decision.max_path_wind_change > 1.0
    assert decision.near_path_blocked is False


def test_update_environment_cost_repairs_locally():
    from app.grid_planner_wa_lpa_star import WALPAStarPlanner
    from app.models import Thresholds

    grid = _grid()
    config = {"weights": {"beta_wind": 80.0, "gamma_headwind": 0.0, "delta_crosswind": 0.0}}
    planner = WALPAStarPlanner(
        grid,
        {"row": 4, "col": 0},
        {"row": 4, "col": 8},
        cost_config=config,
        thresholds=Thresholds(),
    )
    initial = planner.plan()

    changed_cells = []
    for col in range(2, 7):
        grid.u[4, col] = 7.6
        changed_cells.append({"row": 4, "col": col})

    repaired = planner.update_environment_cost(changed_cells)

    assert repaired["replan_type"] == "local"
    assert initial.points != repaired["path"]
    assert repaired["expanded_nodes"] < grid.u.size
    assert repaired["risk_summary"]["max_wind_speed"] < 7.9
