"""Wind-Aware Lifelong Planning A* for coarse UAV route planning.

This module keeps LPA*'s incremental search state and only changes the
heuristic. Edge costs still come from ``cost_evaluator.calculate_edge_cost`` so
A*, LPA* and WA-LPA* share the same distance / wind / terrain / rain model.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field, fields
from math import hypot
from typing import Any, Iterable

import numpy as np

from .cost_evaluator import ensure_cost_config, is_node_flyable
from .grid_planner_lpa_star import LPAStarPlanner, LPAStarResult, Node
from .models import Thresholds
from .wind_provider import WindGrid
from .wind_shear import WindShearEnvironment


EPSILON = 1e-9


@dataclass(frozen=True)
class WALPAStarConfig:
    """Extra knobs used by the wind-aware LPA* layer.

    ``k_wind_align`` controls how strongly tailwind-to-goal lowers the
    heuristic and headwind-to-goal raises it. Keep it modest so the search stays
    stable while still preferring useful wind corridors.
    """

    k_wind_align: float = 0.2
    min_heuristic_scale: float = 0.2
    wind_speed_epsilon: float = 0.3
    wind_change_threshold: float = 1.0
    rain_change_threshold: float = 1.0
    near_path_radius: int = 2
    local_update_radius: int = 1
    global_change_fraction: float = 0.25


@dataclass(frozen=True)
class ReplanDecision:
    replan_type: str
    reason: str
    changed_cells: list[Node]
    max_path_wind_change: float = 0.0
    max_path_rain_change: float | None = None
    near_path_blocked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "replan_type": self.replan_type,
            "reason": self.reason,
            "changed_cells": [{"row": row, "col": col} for row, col in self.changed_cells],
            "max_path_wind_change": round(self.max_path_wind_change, 3),
            "max_path_rain_change": None if self.max_path_rain_change is None else round(self.max_path_rain_change, 3),
            "near_path_blocked": self.near_path_blocked,
        }


def ensure_wa_config(config: Any | None = None) -> WALPAStarConfig:
    if config is None:
        return WALPAStarConfig()
    if isinstance(config, WALPAStarConfig):
        return config
    if isinstance(config, dict):
        source = config.get("wa_lpa_star", config.get("wa_config", config))
        valid_keys = {item.name for item in fields(WALPAStarConfig)}
        values = {key: value for key, value in source.items() if key in valid_keys}
        return WALPAStarConfig(**{**asdict(WALPAStarConfig()), **values})
    return config


def _shape(grid: WindGrid) -> tuple[int, int]:
    return int(len(grid.lats)), int(len(grid.lons))


def _in_bounds(node: Node, grid: WindGrid) -> bool:
    row, col = node
    rows, cols = _shape(grid)
    return 0 <= row < rows and 0 <= col < cols


def _normalize_node(node: Any, grid: WindGrid) -> Node | None:
    if isinstance(node, dict):
        if "lon" in node and "lat" in node:
            return (
                int(np.abs(grid.lats - float(node["lat"])).argmin()),
                int(np.abs(grid.lons - float(node["lon"])).argmin()),
            )
        row = node.get("row", node.get("r", node.get("y")))
        col = node.get("col", node.get("c", node.get("x")))
        if row is None or col is None:
            return None
        candidate = (int(row), int(col))
        return candidate if _in_bounds(candidate, grid) else None

    if hasattr(node, "row") and hasattr(node, "col"):
        candidate = (int(node.row), int(node.col))
        return candidate if _in_bounds(candidate, grid) else None

    try:
        first, second = node[:2]
    except (TypeError, IndexError):
        return None

    first_f, second_f = float(first), float(second)
    row, col = int(round(first_f)), int(round(second_f))
    if abs(first_f - row) <= EPSILON and abs(second_f - col) <= EPSILON and _in_bounds((row, col), grid):
        return row, col

    return (
        int(np.abs(grid.lats - second_f).argmin()),
        int(np.abs(grid.lons - first_f).argmin()),
    )


def _path_nodes(path: Iterable[Any], grid: WindGrid) -> list[Node]:
    nodes: list[Node] = []
    for item in path:
        node = _normalize_node(item, grid)
        if node is not None and (not nodes or nodes[-1] != node):
            nodes.append(node)
    return nodes


def _rain_array(rain_data: Any | None) -> np.ndarray | None:
    if rain_data is None:
        return None
    values = rain_data
    if isinstance(rain_data, dict):
        values = None
        for key in ("rain", "rain_rate", "apcp", "prate"):
            if key in rain_data:
                values = rain_data[key]
                break
        if values is None:
            return None
    array = np.asarray(values, dtype=float)
    return array if array.ndim >= 2 else None


def _changed_cells(
    old_grid: WindGrid,
    new_grid: WindGrid,
    config: WALPAStarConfig,
    *,
    old_rain_data: Any | None = None,
    new_rain_data: Any | None = None,
    changed_cells: Iterable[Any] | None = None,
) -> list[Node]:
    if changed_cells is not None:
        deduped = {
            node
            for item in changed_cells
            if (node := _normalize_node(item, new_grid)) is not None
        }
        return sorted(deduped)

    old_speed = np.hypot(old_grid.u, old_grid.v)
    new_speed = np.hypot(new_grid.u, new_grid.v)
    mask = np.abs(new_speed - old_speed) > max(config.wind_change_threshold * 0.25, EPSILON)

    old_rain = _rain_array(old_rain_data)
    new_rain = _rain_array(new_rain_data)
    if old_rain is not None and new_rain is not None and old_rain.shape == new_rain.shape:
        mask |= np.abs(new_rain - old_rain) > max(config.rain_change_threshold * 0.25, EPSILON)

    return [tuple(map(int, item)) for item in np.argwhere(mask)]


def _near_path(changed: Iterable[Node], path_nodes: list[Node], radius: int) -> bool:
    if not path_nodes:
        return False
    for row, col in changed:
        for path_row, path_col in path_nodes:
            if max(abs(row - path_row), abs(col - path_col)) <= radius:
                return True
    return False


def _near_path_nodes(path_nodes: list[Node], grid: WindGrid, radius: int) -> set[Node]:
    rows, cols = _shape(grid)
    nodes: set[Node] = set()
    for row, col in path_nodes:
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                candidate = (row + dr, col + dc)
                if 0 <= candidate[0] < rows and 0 <= candidate[1] < cols:
                    nodes.add(candidate)
    return nodes


def should_replan(
    old_grid: WindGrid,
    new_grid: WindGrid,
    current_path: Iterable[Any],
    config: Any | None = None,
    *,
    old_rain_data: Any | None = None,
    new_rain_data: Any | None = None,
    new_terrain_data: Any | None = None,
    changed_cells: Iterable[Any] | None = None,
    cost_config: Any | None = None,
) -> ReplanDecision:
    """Decide whether a WA-LPA* repair should be skipped, local or global.

    - Path wind/rain changes over thresholds trigger replanning.
    - New no-fly cells near the current path trigger replanning.
    - Large changed areas trigger a global reset; small near-path changes use
      LPA*'s local repair. Far-away small changes return ``none``.
    """

    wa_config = ensure_wa_config(config)
    cost_cfg = ensure_cost_config(cost_config if cost_config is not None else config)
    path = _path_nodes(current_path, new_grid)
    changed = _changed_cells(
        old_grid,
        new_grid,
        wa_config,
        old_rain_data=old_rain_data,
        new_rain_data=new_rain_data,
        changed_cells=changed_cells,
    )

    max_path_wind_change = 0.0
    for row, col in path:
        old_speed = float(hypot(old_grid.u[row, col], old_grid.v[row, col]))
        new_speed = float(hypot(new_grid.u[row, col], new_grid.v[row, col]))
        max_path_wind_change = max(max_path_wind_change, abs(new_speed - old_speed))

    max_path_rain_change: float | None = None
    old_rain = _rain_array(old_rain_data)
    new_rain = _rain_array(new_rain_data)
    if old_rain is not None and new_rain is not None and old_rain.shape == new_rain.shape:
        max_path_rain_change = 0.0
        for row, col in path:
            max_path_rain_change = max(max_path_rain_change, abs(float(new_rain[row, col] - old_rain[row, col])))

    near_nodes = _near_path_nodes(path, new_grid, wa_config.near_path_radius)
    near_path_blocked = any(
        not is_node_flyable(node, new_grid, new_terrain_data, new_rain_data, cost_cfg)
        for node in near_nodes
    )

    grid_nodes = max(1, int(np.size(new_grid.u)))
    global_change = len(changed) / grid_nodes >= wa_config.global_change_fraction
    near_change = _near_path(changed, path, wa_config.near_path_radius)

    reasons: list[str] = []
    if max_path_wind_change > wa_config.wind_change_threshold:
        reasons.append("current path wind change exceeded threshold")
    if max_path_rain_change is not None and max_path_rain_change > wa_config.rain_change_threshold:
        reasons.append("current path rain change exceeded threshold")
    if near_path_blocked:
        reasons.append("new no-fly cell appeared near current path")
    if near_change and not reasons:
        reasons.append("environment changed near current path")

    if global_change and (reasons or near_change):
        replan_type = "global"
    elif reasons or near_change:
        replan_type = "local"
    else:
        replan_type = "none"
        reasons.append("changes are far from current path")

    return ReplanDecision(
        replan_type=replan_type,
        reason="; ".join(reasons),
        changed_cells=changed,
        max_path_wind_change=max_path_wind_change,
        max_path_rain_change=max_path_rain_change,
        near_path_blocked=near_path_blocked,
    )


@dataclass
class WALPAStarPlanner(LPAStarPlanner):
    """LPA* with a wind-alignment heuristic.

    The planner still stores:
    - ``g(node)``: current start-to-node best cost.
    - ``rhs(node)``: one-step lookahead cost from the best predecessor.
    - queue key: ``min(g, rhs) + h_wind``.
    """

    wa_config: Any | None = None
    last_replan_type: str = field(default="none", init=False)

    def __post_init__(self) -> None:
        self.wa_config = ensure_wa_config(self.wa_config)
        super().__post_init__()

    def wind_alignment_factor(self, node: Node) -> float:
        row, col = node
        east = float(self.goal[1] - col)
        north = float(-(self.goal[0] - row))
        direction_norm = hypot(east, north)
        if direction_norm <= EPSILON:
            return 0.0
        u = float(self.wind_field.u[row, col])
        v = float(self.wind_field.v[row, col])
        wind_norm = hypot(u, v)
        if wind_norm < self.wa_config.wind_speed_epsilon:
            return 0.0
        xi = (east * u + north * v) / (direction_norm * wind_norm + EPSILON)
        return float(np.clip(xi, -1.0, 1.0))

    def heuristic(self, node: Node) -> float:
        h0 = super().heuristic(node)
        if h0 <= 0:
            return 0.0
        xi = self.wind_alignment_factor(node)
        scale = 1.0 - self.wa_config.k_wind_align * xi
        scale = max(float(self.wa_config.min_heuristic_scale), scale)
        return max(0.0, h0 * scale)

    def best_predecessor(self, node: Node) -> tuple[Node | None, float]:
        """Prefer the wind-aware predecessor only when edge costs are tied.

        LPA*'s rhs value is still the exact minimum edge-cost value. The
        wind-aware heuristic is used here only as a deterministic tie-breaker,
        which helps equal-cost routes lean toward tailwind-to-goal corridors.
        """

        best_node = None
        best_value = float("inf")
        best_bias = float("inf")
        for pred in self.predecessors(node):
            value = self.g_value(pred) + self.edge_cost(pred, node)
            bias = self.heuristic(pred)
            if value < best_value - EPSILON or (abs(value - best_value) <= EPSILON and bias < best_bias):
                best_node = pred
                best_value = value
                best_bias = bias
        return best_node, best_value

    def reset_search(self) -> None:
        """Drop accumulated LPA* state for a deliberately global replan."""
        self.g.clear()
        self.rhs.clear()
        self.queue.clear()
        self.queued_keys.clear()
        self.counter = itertools.count()
        self.expanded_nodes = 0
        self.touched_nodes = 0
        self.queue_pushes = 0
        self.wind_shear_blocked_count = 0
        self._has_planned = False
        self.rhs[self.start] = 0.0
        self._insert_or_update(self.start)

    def expand_changed_cells(self, changed_cells: Iterable[Any]) -> list[Node]:
        changed = [self.normalize_node(node) for node in changed_cells]
        affected: set[Node] = set()
        radius = int(self.wa_config.local_update_radius)
        for row, col in changed:
            for dr in range(-radius, radius + 1):
                for dc in range(-radius, radius + 1):
                    candidate = (row + dr, col + dc)
                    if self.in_bounds(candidate):
                        affected.add(candidate)
        return sorted(affected)

    def format_result(self, result: LPAStarResult, replan_type: str) -> dict[str, Any]:
        return {
            "path": result.points,
            "coarse_path": result.coarse_path,
            "total_cost": result.total_cost,
            "risk_summary": {
                "path_length": result.path_length,
                "max_wind_speed": result.max_wind_speed,
                "max_rain": result.max_rain,
                "cumulative_risk": result.cumulative_risk,
            },
            "replan_type": replan_type,
            "planning_time": result.planning_time_ms,
            "planning_time_ms": result.planning_time_ms,
            "expanded_nodes": result.expanded_nodes,
            "touched_nodes": result.touched_nodes,
            "queue_pushes": result.queue_pushes,
            "wind_shear_blocked_count": result.wind_shear_blocked_count,
        }

    def update_environment_cost(
        self,
        changed_cells: Iterable[Any],
        *,
        wind_field: WindGrid | None = None,
        terrain_data: Any | None = None,
        rain_data: Any | None = None,
        cost_config: Any | None = None,
        replan_type: str = "local",
    ) -> dict[str, Any]:
        """Apply environmental changes and return the repaired WA-LPA* path."""

        if wind_field is not None:
            self.wind_field = wind_field
        if terrain_data is not None:
            self.terrain_data = terrain_data
        if rain_data is not None:
            self.rain_data = rain_data
        if cost_config is not None:
            self.cost_config = ensure_cost_config(cost_config)

        if replan_type == "global":
            self.reset_search()
        else:
            affected = self.expand_changed_cells(changed_cells)
            self.update_environment(changed_nodes=affected)
            replan_type = "local"

        self.last_replan_type = replan_type
        return self.format_result(self.plan(), replan_type)

    def should_replan_with(
        self,
        new_grid: WindGrid,
        *,
        current_path: Iterable[Any] | None = None,
        old_rain_data: Any | None = None,
        new_rain_data: Any | None = None,
        new_terrain_data: Any | None = None,
        changed_cells: Iterable[Any] | None = None,
    ) -> ReplanDecision:
        path = current_path if current_path is not None else self.node_path()
        return should_replan(
            self.wind_field,
            new_grid,
            path,
            self.wa_config,
            old_rain_data=self.rain_data if old_rain_data is None else old_rain_data,
            new_rain_data=new_rain_data,
            new_terrain_data=self.terrain_data if new_terrain_data is None else new_terrain_data,
            changed_cells=changed_cells,
            cost_config=self.cost_config,
        )


def plan_wa_lpa_star(
    wind_field: WindGrid,
    start_node: Any,
    goal_node: Any,
    *,
    thresholds: Thresholds | None = None,
    cost_config: Any | None = None,
    wa_config: Any | None = None,
    terrain_data: Any | None = None,
    rain_data: Any | None = None,
    wind_shear_environment: WindShearEnvironment | None = None,
) -> dict[str, Any]:
    planner = WALPAStarPlanner(
        wind_field,
        start_node,
        goal_node,
        cost_config=cost_config,
        terrain_data=terrain_data,
        rain_data=rain_data,
        thresholds=thresholds,
        wind_shear_environment=wind_shear_environment,
        wa_config=wa_config,
    )
    return planner.format_result(planner.plan(), "none")
