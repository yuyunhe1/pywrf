"""Demo for wind-aware LPA* planning and incremental repair.

Run from repository root:
    python wind-risk-platform/backend/scripts/run_wa_lpa_star_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.grid_planner_wa_lpa_star import WALPAStarPlanner, should_replan
from app.models import Thresholds
from app.routing import plan_route
from app.wind_provider import WindGrid


def make_grid() -> WindGrid:
    rng = np.random.default_rng(0)
    lons = np.arange(11, dtype=float)
    lats = np.arange(10, -1, -1, dtype=float)
    u = 1.0 + rng.normal(0, 2.0, (11, 11))
    v = rng.normal(0, 2.0, (11, 11))
    speed = np.hypot(u, v)
    u = np.where(speed >= 7.5, u / speed * 7.4, u)
    v = np.where(speed >= 7.5, v / speed * 7.4, v)
    return WindGrid(
        lons=lons,
        lats=lats,
        u=u,
        v=v,
        cycle="2026-06-15 00:00 UTC",
        forecast_hour=3,
        level="100m AGL",
        valid_time="2026-06-15 03:00 UTC",
    )


def main() -> None:
    grid = make_grid()
    thresholds = Thresholds()
    start_node = {"row": 5, "col": 0}
    goal_node = {"row": 5, "col": 10}
    start_point = (float(grid.lons[start_node["col"]]), float(grid.lats[start_node["row"]]))
    end_point = (float(grid.lons[goal_node["col"]]), float(grid.lats[goal_node["row"]]))
    cost_config = {
        "weights": {
            "alpha_distance": 1.0,
            "beta_wind": 25.0,
            "gamma_headwind": 0.1,
            "delta_crosswind": 0.05,
            "eta_terrain": 0.0,
            "mu_rain": 0.0,
        }
    }

    astar = plan_route(grid, start_point, end_point, thresholds, cost_config)
    planner = WALPAStarPlanner(
        grid,
        start_node,
        goal_node,
        thresholds=thresholds,
        cost_config=cost_config,
        wa_config={"k_wind_align": 0.3},
    )
    initial = planner.format_result(planner.plan(), "none")
    print("initial_wa_lpa_star:")
    print(f"  astar_baseline_path: {astar['points']}")
    print(f"  path: {initial['path']}")
    print(f"  total_cost: {initial['total_cost']}")
    print(f"  risk_summary: {initial['risk_summary']}")
    print(f"  planning_time_ms: {initial['planning_time_ms']}")

    old_grid = grid
    new_grid = WindGrid(
        lons=grid.lons,
        lats=grid.lats,
        u=grid.u.copy(),
        v=grid.v.copy(),
        cycle=grid.cycle,
        forecast_hour=grid.forecast_hour,
        level=grid.level,
        valid_time=grid.valid_time,
    )
    changed_cells = []
    for row, col in planner.node_path()[1:-1]:
        new_grid.u[row, col] = 7.6
        new_grid.v[row, col] = 0.0
        changed_cells.append({"row": row, "col": col})

    decision = should_replan(old_grid, new_grid, planner.node_path(), planner.wa_config, changed_cells=changed_cells, cost_config=planner.cost_config)
    print("replan_decision:")
    print(f"  {decision.as_dict()}")

    repaired = planner.update_environment_cost(changed_cells, wind_field=new_grid, replan_type=decision.replan_type)
    print("after_environment_update:")
    print(f"  path: {repaired['path']}")
    print(f"  replan_type: {repaired['replan_type']}")
    print(f"  expanded_nodes: {repaired['expanded_nodes']}")
    print(f"  planning_time_ms: {repaired['planning_time_ms']}")


if __name__ == "__main__":
    main()
