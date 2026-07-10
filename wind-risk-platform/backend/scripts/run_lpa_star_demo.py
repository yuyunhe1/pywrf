"""Compare A* baseline with LPA* initial planning and incremental repair.

Run from repository root:
    python wind-risk-platform/backend/scripts/run_lpa_star_demo.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.grid_planner_lpa_star import LPAStarPlanner
from app.models import Thresholds
from app.routing import plan_route
from app.wind_provider import WindGrid


def make_grid() -> WindGrid:
    lons = np.arange(9, dtype=float)
    lats = np.arange(8, -1, -1, dtype=float)
    u = np.ones((9, 9), dtype=float)
    v = np.zeros((9, 9), dtype=float)
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
    cost_config = {
        "weights": {
            "alpha_distance": 1.0,
            "beta_wind": 80.0,
            "gamma_headwind": 0.0,
            "delta_crosswind": 0.0,
            "eta_terrain": 0.0,
            "mu_rain": 0.0,
        }
    }
    start_node = {"row": 4, "col": 0}
    goal_node = {"row": 4, "col": 8}
    start_point = (float(grid.lons[0]), float(grid.lats[4]))
    end_point = (float(grid.lons[8]), float(grid.lats[4]))

    t0 = time.perf_counter()
    astar = plan_route(grid, start_point, end_point, thresholds, cost_config)
    astar_ms = (time.perf_counter() - t0) * 1000

    planner = LPAStarPlanner(grid, start_node, goal_node, cost_config=cost_config, thresholds=thresholds)
    initial = planner.plan()

    print("initial_static_environment:")
    print(f"  astar_time_ms: {astar_ms:.3f}")
    print(f"  astar_points: {astar['points']}")
    print(f"  lpa_time_ms: {initial.planning_time_ms:.3f}")
    print(f"  lpa_points: {initial.points}")
    print(f"  lpa_expanded_nodes: {initial.expanded_nodes}")

    # Simulate a local wind-risk increase along the previous straight path.
    changed_nodes = []
    for col in range(2, 7):
        grid.u[4, col] = 7.6
        changed_nodes.append({"row": 4, "col": col})

    planner.update_environment(changed_nodes=changed_nodes)
    repaired = planner.plan()

    scratch = LPAStarPlanner(grid, start_node, goal_node, cost_config=cost_config, thresholds=thresholds)
    scratch_result = scratch.plan()

    print("after_local_wind_change:")
    print(f"  changed_nodes: {changed_nodes}")
    print(f"  repaired_points: {repaired.points}")
    print(f"  repaired_time_ms: {repaired.planning_time_ms:.3f}")
    print(f"  repaired_expanded_nodes: {repaired.expanded_nodes}")
    print(f"  scratch_points: {scratch_result.points}")
    print(f"  scratch_time_ms: {scratch_result.planning_time_ms:.3f}")
    print(f"  scratch_expanded_nodes: {scratch_result.expanded_nodes}")
    print(f"  repaired_touched_nodes: {repaired.touched_nodes}")
    print(f"  total_grid_nodes: {grid.u.size}")


if __name__ == "__main__":
    main()
