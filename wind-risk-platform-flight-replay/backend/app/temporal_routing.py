"""Forward searches on (position, exact arrival time, incoming direction).

Travel time belongs to each candidate's own geometry, independently of its
weighted cost. Different arrival labels at one cell cannot dominate each other:
an earlier arrival can be unsafe while a later arrival is flyable. No waiting
edges, forecast extrapolation, reverse-time search or static goal rejection.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
import heapq
import itertools
import math
import time

import numpy as np

from .cost_evaluator import cost_config_from_thresholds
from .departure_windows import Interval, _cell_visits, _merge, _vector_issues
from .wind_time import frame_boundaries, WIND_TIME_STEP_SECONDS
from .planning_control import checkpoint
from .planning_limits import DEFAULT_MAX_GRID_CELLS, DEFAULT_MAX_TEMPORAL_LABELS, integer_setting, memory_pressure
from .planning_objective import edge_objective
from .grid_coverage import occupancy_spans, shear_contacts, boundary_contacts
from .route_service import haversine_km
from .routing import NEIGHBOURS
from .wrf_timeline import cell_vector, vector_at
from .wrf_native_schema import NATIVE_WRF_TIME_MODEL


class TemporalSearchExhausted(ValueError):
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


def _unsafe_vectors(times, values, limit, time_model='HOURLY_UV_TEN_MINUTE_HOLD_V1'):
    """The same closed hard limits and open missing-hour gaps as fixed routes."""
    result = [Interval(-math.inf, times[0], True, False), Interval(times[-1], math.inf, False, True)]
    missing, unsafe = _vector_issues(times, values, limit,time_model)
    result.extend(missing)
    result.extend(unsafe)
    return tuple(_merge(result))


@dataclass
class TimedEdge:
    points: tuple
    distance_km: float
    duration: float
    visits: list
    forbidden: tuple
    starts: tuple
    spans: tuple
    contacts: tuple

    def allowed(self, departure):
        index = bisect_right(self.starts, departure) - 1
        return index < 0 or not self.forbidden[index].contains(departure)


class TemporalWeather:
    """Cache cell hazards once, then shift them by each edge's cell occupancy."""

    def __init__(self, dataset, request, cost_config):
        if not dataset.frames:
            raise ValueError(dataset.error or "缺少可用的小时风场")
        self.dataset, self.request = dataset, request
        self.grid = dataset.frames[0][1]
        self.times = [stamp for stamp, _ in dataset.frames]
        self.speed = request.average_ground_speed_mps
        self.config = cost_config_from_thresholds(request.thresholds, cost_config)
        settings = request.wind_shear
        self.shear_limit = (settings.horizontal.hard_delta_wind_vector_ms
                            if settings.enabled and settings.horizontal.enabled and settings.horizontal.hard_constraint_enabled else None)
        self.cell_hazards, self.shear_hazards = {}, {}
        self.edges = OrderedDict()
        # A direction-independent lower bound over ALL supported forecast
        # times, including every interpolated ten-minute held frame.
        minimum = np.full(self.grid.u.shape, np.inf)
        for index, (stamp, grid) in enumerate(dataset.frames):
            magnitude = np.hypot(grid.u, grid.v)
            minimum = np.minimum(minimum, np.where(np.isfinite(magnitude), magnitude, np.inf))
            if dataset.time_model!=NATIVE_WRF_TIME_MODEL and index and math.isclose(stamp - dataset.frames[index - 1][0], 3600, abs_tol=.001):
                previous = dataset.frames[index - 1][1]
                du, dv = grid.u - previous.u, grid.v - previous.v
                for offset in range(WIND_TIME_STEP_SECONDS, 3600, WIND_TIME_STEP_SECONDS):
                    fraction = offset / 3600
                    value = np.hypot(previous.u + fraction * du, previous.v + fraction * dv)
                    minimum = np.minimum(minimum, np.where(np.isfinite(value), value, np.inf))
        risk = np.clip((minimum - request.thresholds.safe) / max(1e-9, request.thresholds.danger - request.thresholds.safe), 0, 1)
        risk[~np.isfinite(minimum)] = 0
        self.minimum_cost = self.config.weights.alpha_distance + self.config.weights.beta_wind * risk

    def hazards(self, cell, other=None):
        cache, key = (self.cell_hazards, cell) if other is None else (self.shear_hazards, tuple(sorted((cell, other))))
        if key not in cache:
            values = []
            for _, grid in self.dataset.frames:
                u, v = float(grid.u[cell]), float(grid.v[cell])
                if other is not None:
                    u, v = u - float(grid.u[other]), v - float(grid.v[other])
                values.append((u, v))
            cache[key] = _unsafe_vectors(self.times, values, self.request.thresholds.danger if other is None else self.shear_limit,self.dataset.time_model)
        return cache[key]

    def edge(self, start, end):
        key = (start, end)
        if key in self.edges:
            self.edges.move_to_end(key)
            return self.edges[key]
        checkpoint()
        distance = haversine_km(start, end)
        visits = _cell_visits([start, end], self.grid, self.speed)
        forbidden = []
        for visit in visits:
            for interval in self.hazards((visit.row, visit.col)):
                forbidden.append(Interval(interval.start - visit.leave, interval.end - visit.enter,
                                          interval.left_closed, interval.right_closed))
        if self.shear_limit is not None:
            for a, b, contact in shear_contacts(visits):
                for interval in self.hazards(a, b):
                    forbidden.append(Interval(interval.start - contact.leave, interval.end - contact.enter,
                                              interval.left_closed, interval.right_closed))
        forbidden = tuple(_merge(forbidden))
        result = TimedEdge((start, end), distance, distance * 1000 / self.speed,
                           visits, forbidden, tuple(item.start for item in forbidden), occupancy_spans(visits),
                           boundary_contacts([start,end], self.grid, self.speed))
        self.edges[key] = result
        if len(self.edges) > 30000:
            self.edges.popitem(last=False)
        return result

    def cost(self, edge, departure):
        return edge_objective(edge, departure,
                              lambda stamp,row,col: cell_vector(self.dataset,stamp,row,col), self.config)

    def route_cost(self, points, *, require_allowed=True):
        departure = self.request.departure_at.timestamp()
        elapsed, costs = 0., []
        for left, right in zip(points, points[1:]):
            a, b = tuple(left[:2]), tuple(right[:2])
            if a == b:
                continue
            edge = self.edge(a, b)
            if require_allowed and not edge.allowed(departure + elapsed):
                return math.inf
            costs.append(self.cost(edge, departure + elapsed))
            elapsed += edge.duration
        return math.fsum(costs)


class TemporalPlanner:
    """A* or LPA* consistency search on a lazily materialized time graph.

    LPA* uses g/rhs and predecessor relaxations; WA-LPA* also applies the existing
    wind-alignment heuristic at the label's arrival time. A new departure creates
    a new graph, since shifting time changes every edge's weather.
    """

    def __init__(self, weather, start, end, bbox, max_grid_cells=DEFAULT_MAX_GRID_CELLS):
        self.weather, self.grid = weather, weather.grid
        self.start, self.end = tuple(start), tuple(end)
        if self.start == self.end:
            raise ValueError("起点和终点必须不同")
        for lon, lat in (self.start, self.end):
            if not (math.isfinite(lon) and math.isfinite(lat) and min(self.grid.lons) <= lon <= max(self.grid.lons)
                    and min(self.grid.lats) <= lat <= max(self.grid.lats)):
                raise ValueError("起点或终点超出风场空间覆盖范围")
        west, south, east, north = bbox
        rows = np.flatnonzero((self.grid.lats >= south) & (self.grid.lats <= north)).tolist()
        cols = np.flatnonzero((self.grid.lons >= west) & (self.grid.lons <= east)).tolist()
        self.stride = (1 if max_grid_cells <= 0 else
                       max(1, math.ceil(math.sqrt(len(rows) * len(cols) / max_grid_cells))))
        # Retain exact endpoint neighbourhoods even when the main lattice is thinned.
        self.rows = sorted(set(rows[::self.stride] + [rows[-1]] + [int(np.abs(self.grid.lats - p[1]).argmin()) for p in (start, end)]))
        self.cols = sorted(set(cols[::self.stride] + [cols[-1]] + [int(np.abs(self.grid.lons - p[0]).argmin()) for p in (start, end)]))
        self.row_index, self.col_index = {v: i for i, v in enumerate(self.rows)}, {v: i for i, v in enumerate(self.cols)}
        self.start_id, self.goal_id = (-1, -1), (-2, -2)
        self.anchors = {}
        self.start_near, self.end_near = self.near(start), self.near(end)
        for node in self.start_near:
            if haversine_km(self.point(node), self.start) * 1000 < 1e-6:
                self.start_id = node
        for node in self.end_near:
            if haversine_km(self.point(node), self.end) * 1000 < 1e-6:
                self.goal_id = node
        self.anchors = {self.start_id: self.start, self.goal_id: self.end}
        self.lower_bounds = None

    def near(self, point):
        rows = sorted(self.rows, key=lambda r: abs(self.grid.lats[r] - point[1]))[:2]
        cols = sorted(self.cols, key=lambda c: abs(self.grid.lons[c] - point[0]))[:2]
        return {(r, c) for r in rows for c in cols}

    def point(self, node):
        if node in self.anchors:
            return self.anchors[node]
        if node == (-1, -1):
            return self.start
        if node == (-2, -2):
            return self.end
        return float(self.grid.lons[node[1]]), float(self.grid.lats[node[0]])

    def neighbours(self, node):
        if node == self.goal_id:
            return []
        if node == (-1, -1):
            result = list(self.start_near)
            if self.start_near & self.end_near:
                result.append(self.goal_id)
            return result
        r, c = self.row_index[node[0]], self.col_index[node[1]]
        result = [(self.rows[r + dr], self.cols[c + dc]) for dr, dc in NEIGHBOURS
                  if 0 <= r + dr < len(self.rows) and 0 <= c + dc < len(self.cols)]
        if node in self.end_near and self.goal_id not in result:
            result.append(self.goal_id)
        return result

    def prepare_heuristic(self):
        """Reverse 2-D lower costs guide the forward 3-D search, not feasibility.

        Each edge integrates the minimum possible wind preference over its raw
        cells. Ignoring time and hard obstacles is optimistic, so this remains
        admissible for A*/LPA*, including sparse-lattice edges and real anchors.
        """
        if self.lower_bounds is not None:
            return
        bounds, queue, costs = {self.goal_id: 0.0}, [(0.0, self.goal_id)], {}
        next_memory_check = 4096
        while queue:
            checkpoint()
            if len(costs) >= next_memory_check:
                next_memory_check = len(costs) + 4096
                pressure = memory_pressure()
                if pressure:
                    raise TemporalSearchExhausted("搜索达到可用内存资源上限；已验收结果将保留", {
                        **pressure, "search_phase": "heuristic", "heuristic_nodes": len(bounds)})
            value, node = heapq.heappop(queue)
            if value != bounds[node]:
                continue
            if node == self.goal_id:
                previous = list(self.end_near)
                if node[0] >= 0:
                    previous.extend(self._lattice_neighbours(node))
                if self.start_near & self.end_near and self.start_id == (-1, -1):
                    previous.append(self.start_id)
            elif node == (-1, -1):
                previous = []
            else:
                previous = self._lattice_neighbours(node)
                if node in self.start_near and self.start_id == (-1, -1):
                    previous.append(self.start_id)
            for parent in previous:
                if parent == node or parent == self.goal_id:
                    continue
                a, b = self.point(parent), self.point(node)
                key = tuple(sorted((a, b)))
                if key not in costs:
                    visits = _cell_visits([a, b], self.grid, self.weather.speed)
                    costs[key] = sum((visit.leave - visit.enter) * self.weather.speed / 1000
                                     * self.weather.minimum_cost[visit.row, visit.col] for visit in visits)
                candidate = value + costs[key]
                if candidate < bounds.get(parent, math.inf):
                    bounds[parent] = candidate
                    heapq.heappush(queue, (candidate, parent))
        self.lower_bounds = bounds

    def _lattice_neighbours(self, node):
        r, c = self.row_index[node[0]], self.col_index[node[1]]
        return [(self.rows[r + dr], self.cols[c + dc]) for dr, dc in NEIGHBOURS
                if 0 <= r + dr < len(self.rows) and 0 <= c + dc < len(self.cols)]

    def search(self, planner_type="astar", heuristic_weight=1.0, incumbent_cost=math.inf):
        if planner_type not in ("astar", "a_star", "lpa_star", "wa_lpa_star"):
            raise ValueError("不支持的规划算法")
        incremental = planner_type in ("lpa_star", "wa_lpa_star")
        self.last_search_diagnostics = {"expanded_nodes": 0, "arrival_labels": 0}
        departure = self.weather.request.departure_at.timestamp()
        start_wind = vector_at(self.weather.dataset, departure, *self.start)
        if start_wind is None:
            raise ValueError("起飞时刻的起点缺少可用风场，无法规划完整航程")
        if math.hypot(*start_wind) >= self.weather.request.thresholds.danger:
            raise ValueError("起飞时刻的起点风速达到硬上限，无法起飞")
        self.prepare_heuristic()
        last_time = self.weather.times[-1]
        # An explicit zero still supports controlled, isolated unlimited runs.
        max_labels = integer_setting("ROUTE_PLAN_MAX_TEMPORAL_LABELS", DEFAULT_MAX_TEMPORAL_LABELS)
        weights = self.weather.config.weights
        started, counter = time.monotonic(), itertools.count()
        # State time is exact travel time, never the accumulated weighted cost.
        initial = (self.start_id, 0.0, None)
        g, rhs, parents = {}, {initial: 0.0}, {initial: None}
        if not incremental:
            g[initial] = 0.0
        queue, queued, successors, predecessors = [], {}, {}, {}
        expanded = 0

        def priority(state):
            node, elapsed, _ = state
            value = min(g.get(state, math.inf), rhs.get(state, math.inf)) if incremental else g.get(state, math.inf)
            point = self.point(node)
            lower = self.lower_bounds.get(node, weights.alpha_distance * haversine_km(point, self.end))
            scale = 1.0
            if planner_type == "wa_lpa_star" and lower > 0:
                wind = vector_at(self.weather.dataset, departure + elapsed, *point)
                if wind and math.hypot(*wind) > .3:
                    dx = (self.end[0] - point[0]) * math.cos(math.radians(point[1]))
                    dy = self.end[1] - point[1]
                    alignment = (wind[0] * dx + wind[1] * dy) / (math.hypot(*wind) * math.hypot(dx, dy))
                    scale = max(.2, 1 - .2 * alignment)
            return value + heuristic_weight * scale * lower, value

        def push(state):
            key = priority(state)
            queued[state] = key
            heapq.heappush(queue, (*key, next(counter), state))

        def update(state):
            if state != initial:
                best_parent, value = min(((p, g.get(p, math.inf) + cost) for p, cost in predecessors[state].items()),
                                         key=lambda item: item[1])
                rhs[state] = value
                parents[state] = best_parent
            queued.pop(state, None)
            if g.get(state, math.inf) != rhs.get(state, math.inf):
                push(state)

        push(initial)
        while queue:
            checkpoint()
            first, second, _, state = heapq.heappop(queue)
            if queued.get(state) != (first, second):
                continue
            del queued[state]
            if incremental:
                if g.get(state, math.inf) > rhs[state]:
                    g[state] = rhs[state]
                else:
                    g[state] = math.inf
                    update(state)
                    for following in successors.get(state, {}):
                        update(following)
                    continue
            node, elapsed, previous = state
            expanded += 1
            if expanded % 512 == 0:
                self.last_search_diagnostics = {"expanded_nodes": expanded, "arrival_labels": len(rhs),
                                                "planning_time_ms": (time.monotonic() - started) * 1000}
                pressure = memory_pressure()
                if pressure:
                    raise TemporalSearchExhausted("搜索达到可用内存资源上限；已验收结果将保留", {
                        **pressure, "expanded_nodes": expanded, "arrival_labels": len(rhs),
                        "planning_time_ms": (time.monotonic() - started) * 1000})
            bound = incumbent_cost() if callable(incumbent_cost) else incumbent_cost
            if g[state] + self.lower_bounds.get(node, 0.) >= bound:
                continue
            if node == self.goal_id:
                chain, current = [], state
                while current is not None:
                    chain.append(self.point(current[0]))
                    current = parents[current]
                points = list(reversed(chain))
                return {"points": points, "cost": g[state], "distance_km": elapsed * self.weather.speed / 1000,
                        "planner_type": planner_type, "time_aware": True, "search_direction": "forward",
                        "search_grid_stride": self.stride, "expanded_nodes": expanded,
                        "arrival_labels": len(rhs), "planning_time_ms": (time.monotonic() - started) * 1000}
            for following in self.neighbours(node):
                start, end = self.point(node), self.point(following)
                if start == end:
                    continue
                edge = self.weather.edge(start, end)
                if edge.duration <= 0:
                    continue
                arrival = elapsed + edge.duration
                if departure + arrival + haversine_km(end, self.end) * 1000 / self.weather.speed > last_time:
                    continue
                if not edge.allowed(departure + elapsed):
                    continue
                direction = (following[0] - node[0], following[1] - node[1]) if node[0] >= 0 and following[0] >= 0 else None
                edge_cost = self.weather.cost(edge, departure + elapsed)
                if not math.isfinite(edge_cost):
                    continue
                target = (following, arrival, direction)
                new_cost = g[state] + edge_cost
                if new_cost + self.lower_bounds.get(following, 0.) >= bound:
                    continue
                if max_labels and target not in rhs and len(rhs) >= max_labels:
                    raise TemporalSearchExhausted("跨时刻搜索达到资源上限；已验收结果将保留", {
                        "limit_type": "arrival_labels", "max_arrival_labels": max_labels,
                        "expanded_nodes": expanded, "arrival_labels": len(rhs),
                        "planning_time_ms": (time.monotonic() - started) * 1000})
                if incremental:
                    successors.setdefault(state, {})[target] = edge_cost
                    predecessors.setdefault(target, {})[state] = edge_cost
                    update(target)
                elif new_cost < g.get(target, math.inf):
                    rhs[target] = new_cost
                    g[target] = new_cost
                    parents[target] = state
                    push(target)
        raise TemporalSearchExhausted("在所选起飞时间、地速和预报覆盖范围内，未找到满足全程风速及风切变限制的更优航线", {
            "expanded_nodes": expanded, "arrival_labels": len(rhs),
            "planning_time_ms": (time.monotonic() - started) * 1000})
