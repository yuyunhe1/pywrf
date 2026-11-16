"""Print a decomposed edge-cost example for the shared cost evaluator.

Run from repository root:
    python wind-risk-platform/backend/scripts/print_edge_cost_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.cost_evaluator import calculate_edge_cost, decompose_wind_along_edge, ensure_cost_config
from app.wind_provider import get_grid


def main() -> None:
    grid = get_grid("2026-06-15 06:00 UTC", 3, "100m AGL", None)
    node_from = (10, 10)
    node_to = (10, 11)

    terrain = np.zeros_like(grid.u)
    rain = np.zeros_like(grid.u)
    rain[node_to] = 2.0

    config = ensure_cost_config(
        {
            "weights": {
                "alpha_distance": 1.0,
                "beta_wind": 12.0,
                "gamma_headwind": 0.7,
                "delta_crosswind": 0.2,
                "eta_terrain": 5.0,
                "mu_rain": 8.0,
            },
            "thresholds": {
                "safe_wind_speed": 1.5,
                "max_wind_speed": 7.9,
                "max_rain": 10.0,
                "rain_blocking": False,
                "min_agl_height": 60.0,
            },
        }
    )

    u = float(grid.u[node_to])
    v = float(grid.v[node_to])
    components = decompose_wind_along_edge(node_from, node_to, u, v)
    cost = calculate_edge_cost(node_from, node_to, grid, terrain, rain, config)

    print("wind_components:")
    for key in ("headwind", "tailwind", "crosswind"):
        print(f"  {key}: {components[key]:.4f}")

    print("edge_cost:")
    for key in (
        "distance_cost",
        "wind_speed_cost",
        "headwind_cost",
        "crosswind_cost",
        "terrain_cost",
        "rain_cost",
        "total_cost",
    ):
        print(f"  {key}: {cost.as_dict()[key]:.4f}")
    print(f"  blocked: {cost.blocked}")
    print(f"  max_wind_speed: {config.thresholds.max_wind_speed:.1f} m/s")


if __name__ == "__main__":
    main()
