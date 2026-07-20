"""Wind-aware A* routing on a regular lon/lat wind grid."""

from __future__ import annotations

import heapq
from math import acos, degrees, hypot

import numpy as np

from .cost_evaluator import calculate_edge_cost, cost_config_from_thresholds, evaluate_node_flyability
from .models import Thresholds
from .route_service import anchor_route_endpoints, haversine_km, risk_name
from .wind_provider import WindGrid, point_value
from .wind_shear import WindShearBlockedError, WindShearEnvironment


# Row points south; column points east.  State retains the previous direction so
# turn cost is part of the graph rather than an after-the-fact cosmetic change.
NEIGHBOURS = tuple((dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0))


def _nearest_index(values: np.ndarray, value: float) -> int:
    return int(np.abs(values - value).argmin())


def _turn_cost(previous: tuple[int, int] | None, current: tuple[int, int]) -> float:
    if previous is None:
        return 0.0
    dot = previous[0] * current[0] + previous[1] * current[1]
    denominator = hypot(*previous) * hypot(*current)
    angle = degrees(acos(float(np.clip(dot / denominator, -1, 1))))
    # Mild changes are free; sharp turns become increasingly unattractive.
    return max(0.0, angle - 25) / 20


def _cell_point(grid: WindGrid, row: int, col: int) -> tuple[float, float]:
    return float(grid.lons[col]), float(grid.lats[row])


def _segment_risk(points: list[tuple[float, float]], grid: WindGrid, thresholds: Thresholds) -> list[dict]:
    segments = []
    for start, end in zip(points, points[1:]):
        # 如果起点和终点完全一致（例如被强制替换导致），可以跳过或当做极短线段
        if start == end:
            continue
        midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        wind = point_value(grid, *midpoint)
        segments.append({
            "start": start, "end": end, "wind_speed": round(wind["wind_speed"], 2),
            "risk": risk_name(wind["wind_speed"], thresholds),
            "distance_km": round(haversine_km(start, end), 3),
        })
    return segments


def plan_route(
    grid: WindGrid,
    start: tuple[float, float],
    end: tuple[float, float],
    thresholds: Thresholds,
    cost_config=None,
    terrain_data=None,
    rain_data=None,
    wind_shear_environment: WindShearEnvironment | None = None,
) -> dict:
    """Find a safe 8-neighbour path; one requested level means altitude is constant."""
    start_node = (_nearest_index(grid.lats, start[1]), _nearest_index(grid.lons, start[0]))
    end_node = (_nearest_index(grid.lats, end[1]), _nearest_index(grid.lons, end[0]))
    cost_config = cost_config_from_thresholds(thresholds, cost_config)
    start_check = evaluate_node_flyability(
        start_node, grid, terrain_data, rain_data, cost_config, wind_shear_environment
    )
    end_check = evaluate_node_flyability(
        end_node, grid, terrain_data, rain_data, cost_config, wind_shear_environment
    )
    if not start_check["is_flyable"] or not end_check["is_flyable"]:
        raise ValueError("起点或终点位于四级风及以上（或更高）的禁飞网格内")

    def heuristic(row: int, col: int) -> float:
        return haversine_km(_cell_point(grid, row, col), _cell_point(grid, *end_node))

    # (estimated total, cost, row, col, previous direction)
    queue = [(heuristic(*start_node), 0.0, *start_node, None)]
    parents: dict[tuple[int, int, tuple[int, int] | None], tuple[int, int, tuple[int, int] | None] | None] = {}
    best: dict[tuple[int, int, tuple[int, int] | None], float] = {(start_node[0], start_node[1], None): 0.0}
    goal_state = None
    wind_shear_blocked_count = 0
    while queue:
        _, cost, row, col, previous = heapq.heappop(queue)
        state = (row, col, previous)
        if cost != best.get(state):
            continue
        if (row, col) == end_node:
            goal_state = state
            break
        for direction in NEIGHBOURS:
            nr, nc = row + direction[0], col + direction[1]
            if not (0 <= nr < len(grid.lats) and 0 <= nc < len(grid.lons)):
                continue
            edge_cost = calculate_edge_cost(
                (row, col),
                (nr, nc),
                grid,
                terrain_data,
                rain_data,
                cost_config,
                wind_shear_environment,
            )
            if edge_cost.blocked or not np.isfinite(edge_cost.total_cost):
                if edge_cost.reason == "horizontal_wind_shear":
                    wind_shear_blocked_count += 1
                continue
            next_cost = cost + edge_cost.total_cost + _turn_cost(previous, direction)
            next_state = (nr, nc, direction)
            if next_cost < best.get(next_state, float("inf")):
                best[next_state] = next_cost
                parents[next_state] = state
                heapq.heappush(queue, (next_cost + heuristic(nr, nc), next_cost, nr, nc, direction))
    if goal_state is None:
        if wind_shear_blocked_count:
            raise WindShearBlockedError(wind_shear_blocked_count)
        raise ValueError("在当前四级风及以上（或更高）的禁飞限制下，未找到安全的飞行航线")
    nodes = []
    state = goal_state
    while state is not None:
        nodes.append((state[0], state[1]))
        state = parents.get(state)
    nodes.reverse()
    # Keep only meaningful bends; grid A* often produces repeated collinear cells.
    simplified = [nodes[0]]
    for index in range(1, len(nodes) - 1):
        a, b, c = simplified[-1], nodes[index], nodes[index + 1]
        if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
            simplified.append(b)
    if simplified[-1] != nodes[-1]:
        simplified.append(nodes[-1])

    grid_points = [_cell_point(grid, row, col) for row, col in simplified]
    points = anchor_route_endpoints(grid_points, start, end)
    shear_analysis_points = anchor_route_endpoints(
        [_cell_point(grid, row, col) for row, col in nodes],
        start,
        end,
    )
        
    segments = _segment_risk(points, grid, thresholds)
    return {
        "points": points, "segments": segments, "cost": round(goal_state and best[goal_state], 3),
        "distance_km": round(sum(item["distance_km"] for item in segments), 3),
        "level": grid.level,
        "search_wind_shear_blocked_edges": wind_shear_blocked_count,
        "shear_analysis_points": shear_analysis_points,
    }
