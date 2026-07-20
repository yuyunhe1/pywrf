"""Lifelong Planning A* on the platform wind grid.

LPA* keeps two values for each node:

- g(node): the current best-known cost from the start to this node.
- rhs(node): the one-step lookahead cost, i.e. the best predecessor cost plus
  the current edge cost into this node.

A node is locally consistent when g(node) == rhs(node). Only inconsistent nodes
are stored in the priority queue. When wind/terrain/rain changes, callers update
the affected nodes and LPA* repairs the existing search tree instead of clearing
all g/rhs values and planning from scratch.
"""

from __future__ import annotations

import heapq
import itertools
import time
from dataclasses import dataclass, field
from math import inf
from typing import Any, Iterable

import numpy as np

from .cost_evaluator import calculate_edge_cost, cost_config_from_thresholds, ensure_cost_config, is_node_flyable
from .models import Thresholds
from .route_service import haversine_km
from .wind_provider import WindGrid
from .wind_shear import WindShearEnvironment


Node = tuple[int, int]
NEIGHBOURS: tuple[Node, ...] = tuple((dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0))
INF_KEY = (float("inf"), float("inf"))
EPSILON = 1e-9


@dataclass
class LPAStarResult:
    coarse_path: list[dict[str, float | int | str | None]]
    points: list[tuple[float, float]]
    path_length: float
    total_cost: float
    max_wind_speed: float
    max_rain: float | None
    cumulative_risk: float
    planning_time_ms: float
    expanded_nodes: int
    touched_nodes: int
    queue_pushes: int
    reused_search: bool
    wind_shear_blocked_count: int = 0


@dataclass
class LPAStarPlanner:
    """Incremental grid planner that reuses g/rhs between environment updates."""

    wind_field: WindGrid
    start_node: Any
    goal_node: Any
    cost_config: Any | None = None
    terrain_data: Any | None = None
    rain_data: Any | None = None
    thresholds: Thresholds | None = None
    wind_shear_environment: WindShearEnvironment | None = None

    g: dict[Node, float] = field(default_factory=dict, init=False)
    rhs: dict[Node, float] = field(default_factory=dict, init=False)
    queue: list[tuple[float, float, int, Node]] = field(default_factory=list, init=False)
    queued_keys: dict[Node, tuple[float, float]] = field(default_factory=dict, init=False)
    counter: itertools.count = field(default_factory=itertools.count, init=False)
    expanded_nodes: int = field(default=0, init=False)
    touched_nodes: int = field(default=0, init=False)
    queue_pushes: int = field(default=0, init=False)
    wind_shear_blocked_count: int = field(default=0, init=False)
    _has_planned: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.start = self.normalize_node(self.start_node)
        self.goal = self.normalize_node(self.goal_node)
        self.cost_config = cost_config_from_thresholds(self.thresholds, self.cost_config)
        self.rhs[self.start] = 0.0
        self._insert_or_update(self.start)

    def normalize_node(self, node: Any) -> Node:
        """Accept row/col, x/y or lon/lat and normalize to (row, col)."""
        if isinstance(node, dict):
            if "lon" in node and "lat" in node:
                return (
                    int(np.abs(self.wind_field.lats - float(node["lat"])).argmin()),
                    int(np.abs(self.wind_field.lons - float(node["lon"])).argmin()),
                )
            row = node.get("row", node.get("r", node.get("y")))
            col = node.get("col", node.get("c", node.get("x")))
            if row is None or col is None:
                raise ValueError("LPA* node dict must contain lon/lat, row/col, r/c, or y/x")
            return int(row), int(col)

        if hasattr(node, "row") and hasattr(node, "col"):
            return int(node.row), int(node.col)

        first, second = node[:2]
        row, col = int(round(first)), int(round(second))
        if float(first).is_integer() and float(second).is_integer() and self.in_bounds((row, col)):
            return row, col

        # Fallback: treat a numeric pair outside row/col range as (lon, lat).
        lon, lat = float(first), float(second)
        return (
            int(np.abs(self.wind_field.lats - lat).argmin()),
            int(np.abs(self.wind_field.lons - lon).argmin()),
        )

    def in_bounds(self, node: Node) -> bool:
        row, col = node
        return 0 <= row < len(self.wind_field.lats) and 0 <= col < len(self.wind_field.lons)

    def g_value(self, node: Node) -> float:
        return self.g.get(node, float("inf"))

    def rhs_value(self, node: Node) -> float:
        return self.rhs.get(node, float("inf"))

    def cell_point(self, node: Node) -> tuple[float, float]:
        row, col = node
        return float(self.wind_field.lons[col]), float(self.wind_field.lats[row])

    def heuristic(self, node: Node) -> float:
        return haversine_km(self.cell_point(node), self.cell_point(self.goal))

    def calculate_key(self, node: Node) -> tuple[float, float]:
        """Priority key = min(g, rhs) + h, then min(g, rhs)."""
        value = min(self.g_value(node), self.rhs_value(node))
        return value + self.heuristic(node), value

    def _key_less(self, left: tuple[float, float], right: tuple[float, float]) -> bool:
        return left[0] < right[0] - EPSILON or (abs(left[0] - right[0]) <= EPSILON and left[1] < right[1] - EPSILON)

    def _keys_equal(self, left: tuple[float, float], right: tuple[float, float]) -> bool:
        return abs(left[0] - right[0]) <= EPSILON and abs(left[1] - right[1]) <= EPSILON

    def _insert_or_update(self, node: Node) -> None:
        key = self.calculate_key(node)
        self.queued_keys[node] = key
        heapq.heappush(self.queue, (key[0], key[1], next(self.counter), node))
        self.queue_pushes += 1

    def _remove_from_queue(self, node: Node) -> None:
        self.queued_keys.pop(node, None)

    def top_key(self) -> tuple[float, float]:
        while self.queue:
            key1, key2, _, node = self.queue[0]
            key = (key1, key2)
            if self._keys_equal(self.queued_keys.get(node, INF_KEY), key):
                return key
            heapq.heappop(self.queue)
        return INF_KEY

    def pop_queue(self) -> tuple[Node, tuple[float, float]] | tuple[None, tuple[float, float]]:
        while self.queue:
            key1, key2, _, node = heapq.heappop(self.queue)
            key = (key1, key2)
            if self._keys_equal(self.queued_keys.get(node, INF_KEY), key):
                self._remove_from_queue(node)
                return node, key
        return None, INF_KEY

    def neighbours(self, node: Node) -> list[Node]:
        row, col = node
        return [
            (row + dr, col + dc)
            for dr, dc in NEIGHBOURS
            if self.in_bounds((row + dr, col + dc))
        ]

    def predecessors(self, node: Node) -> list[Node]:
        return self.neighbours(node)

    def edge_cost(self, node_from: Node, node_to: Node) -> float:
        cost = calculate_edge_cost(
            node_from,
            node_to,
            self.wind_field,
            self.terrain_data,
            self.rain_data,
            self.cost_config,
            self.wind_shear_environment,
        )
        if cost.reason == "horizontal_wind_shear":
            self.wind_shear_blocked_count += 1
        return float("inf") if cost.blocked else cost.total_cost

    def best_predecessor(self, node: Node) -> tuple[Node | None, float]:
        best_node = None
        best_value = float("inf")
        for pred in self.predecessors(node):
            value = self.g_value(pred) + self.edge_cost(pred, node)
            if value < best_value:
                best_node = pred
                best_value = value
        return best_node, best_value

    def update_vertex(self, node: Node) -> None:
        """Refresh rhs(node) and queue membership after edge-cost changes."""
        self.touched_nodes += 1
        if node != self.start:
            _, value = self.best_predecessor(node)
            self.rhs[node] = value
        self._remove_from_queue(node)
        if abs(self.g_value(node) - self.rhs_value(node)) > EPSILON:
            self._insert_or_update(node)

    def compute_shortest_path(self, max_iterations: int | None = None) -> int:
        """Repair inconsistent nodes until the goal is locally consistent."""
        iterations = 0
        while self._key_less(self.top_key(), self.calculate_key(self.goal)) or abs(self.rhs_value(self.goal) - self.g_value(self.goal)) > EPSILON:
            if max_iterations is not None and iterations >= max_iterations:
                break
            node, old_key = self.pop_queue()
            if node is None:
                break
            new_key = self.calculate_key(node)
            if self._key_less(old_key, new_key):
                self._insert_or_update(node)
                continue
            if self.g_value(node) > self.rhs_value(node):
                self.g[node] = self.rhs_value(node)
                self.expanded_nodes += 1
                for succ in self.neighbours(node):
                    self.update_vertex(succ)
            else:
                self.g[node] = float("inf")
                self.expanded_nodes += 1
                self.update_vertex(node)
                for succ in self.neighbours(node):
                    self.update_vertex(succ)
            iterations += 1
        self._has_planned = True
        return iterations

    def update_environment(
        self,
        *,
        wind_field: WindGrid | None = None,
        terrain_data: Any | None = None,
        rain_data: Any | None = None,
        changed_nodes: Iterable[Any] | None = None,
        cost_config: Any | None = None,
        wind_shear_environment: WindShearEnvironment | None = None,
    ) -> None:
        """Apply changed grids/costs and mark only affected vertices inconsistent."""
        if wind_field is not None:
            self.wind_field = wind_field
        if terrain_data is not None:
            self.terrain_data = terrain_data
        if rain_data is not None:
            self.rain_data = rain_data
        if cost_config is not None:
            self.cost_config = ensure_cost_config(cost_config)
        if wind_shear_environment is not None:
            self.wind_shear_environment = wind_shear_environment

        if changed_nodes is None:
            changed = [self.start, self.goal]
        else:
            changed = [self.normalize_node(node) for node in changed_nodes]

        affected: set[Node] = set()
        for node in changed:
            if not self.in_bounds(node):
                continue
            affected.add(node)
            affected.update(self.neighbours(node))
        for node in affected:
            self.update_vertex(node)

    def node_record(self, node: Node) -> dict[str, float | int | str | None]:
        row, col = node
        return {
            "row": row,
            "col": col,
            "lon": float(self.wind_field.lons[col]),
            "lat": float(self.wind_field.lats[row]),
            "height": getattr(self.wind_field, "level", None),
            "forecast_time": getattr(self.wind_field, "valid_time", None),
        }

    def node_path(self) -> list[Node]:
        if not np.isfinite(self.g_value(self.goal)):
            return []
        path = [self.goal]
        seen = {self.goal}
        current = self.goal
        while current != self.start:
            pred, value = self.best_predecessor(current)
            if pred is None or not np.isfinite(value) or pred in seen:
                return []
            path.append(pred)
            seen.add(pred)
            current = pred
        path.reverse()
        return path

    def simplify_path(self, path: list[Node]) -> list[Node]:
        if len(path) <= 2:
            return path
        simplified = [path[0]]
        for index in range(1, len(path) - 1):
            previous = simplified[-1]
            current = path[index]
            nxt = path[index + 1]
            if (current[0] - previous[0], current[1] - previous[1]) != (nxt[0] - current[0], nxt[1] - current[1]):
                simplified.append(current)
        simplified.append(path[-1])
        return simplified

    def path_metrics(self, path: list[Node]) -> tuple[float, float, float, float | None, float]:
        if len(path) < 2:
            return 0.0, 0.0, 0.0, None, 0.0
        path_length = 0.0
        total_cost = 0.0
        max_wind = 0.0
        max_rain = None
        cumulative_risk = 0.0
        for node in path:
            row, col = node
            max_wind = max(max_wind, float(np.hypot(self.wind_field.u[row, col], self.wind_field.v[row, col])))
            if self.rain_data is not None:
                rain_source = self.rain_data.get("rain", self.rain_data.get("apcp", self.rain_data.get("prate"))) if isinstance(self.rain_data, dict) else self.rain_data
                if rain_source is not None:
                    rain_value = float(np.asarray(rain_source)[row, col])
                    max_rain = rain_value if max_rain is None else max(max_rain, rain_value)
        for start, end in zip(path, path[1:]):
            edge = calculate_edge_cost(
                start,
                end,
                self.wind_field,
                self.terrain_data,
                self.rain_data,
                self.cost_config,
                self.wind_shear_environment,
            )
            path_length += edge.distance_cost
            total_cost += edge.total_cost
            cumulative_risk += edge.wind_speed_cost + edge.headwind_cost + edge.crosswind_cost + edge.terrain_cost + edge.rain_cost
        return path_length, total_cost, max_wind, max_rain, cumulative_risk

    def get_path(self) -> LPAStarResult:
        start_time = time.perf_counter()
        path = self.node_path()
        coarse_path = self.simplify_path(path)
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        path_length, total_cost, max_wind, max_rain, cumulative_risk = self.path_metrics(path)
        return LPAStarResult(
            coarse_path=[self.node_record(node) for node in coarse_path],
            points=[self.cell_point(node) for node in coarse_path],
            path_length=round(path_length, 3),
            total_cost=round(total_cost, 3) if np.isfinite(total_cost) else float("inf"),
            max_wind_speed=round(max_wind, 3),
            max_rain=None if max_rain is None else round(max_rain, 3),
            cumulative_risk=round(cumulative_risk, 3),
            planning_time_ms=round(elapsed_ms, 3),
            expanded_nodes=self.expanded_nodes,
            touched_nodes=self.touched_nodes,
            queue_pushes=self.queue_pushes,
            reused_search=self._has_planned,
            wind_shear_blocked_count=self.wind_shear_blocked_count,
        )

    def plan(self) -> LPAStarResult:
        start_time = time.perf_counter()
        before_expanded = self.expanded_nodes
        before_touched = self.touched_nodes
        self.compute_shortest_path()
        result = self.get_path()
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        result.planning_time_ms = round(elapsed_ms, 3)
        result.expanded_nodes = self.expanded_nodes - before_expanded
        result.touched_nodes = self.touched_nodes - before_touched
        return result


def plan_lpa_star(
    wind_field: WindGrid,
    start_node: Any,
    goal_node: Any,
    *,
    thresholds: Thresholds | None = None,
    cost_config: Any | None = None,
    terrain_data: Any | None = None,
    rain_data: Any | None = None,
    wind_shear_environment: WindShearEnvironment | None = None,
) -> dict:
    planner = LPAStarPlanner(
        wind_field,
        start_node,
        goal_node,
        cost_config,
        terrain_data,
        rain_data,
        thresholds,
        wind_shear_environment,
    )
    result = planner.plan()
    return {
        "coarse_path": result.coarse_path,
        "points": result.points,
        "path_length": result.path_length,
        "total_cost": result.total_cost,
        "max_wind_speed": result.max_wind_speed,
        "max_rain": result.max_rain,
        "cumulative_risk": result.cumulative_risk,
        "planning_time_ms": result.planning_time_ms,
        "expanded_nodes": result.expanded_nodes,
        "touched_nodes": result.touched_nodes,
        "wind_shear_blocked_count": result.wind_shear_blocked_count,
    }
