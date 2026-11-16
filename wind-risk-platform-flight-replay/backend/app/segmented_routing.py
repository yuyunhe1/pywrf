"""Ten-minute local, bidirectional space/time search (no waiting edges).

Forward labels have exact passage times. Reverse labels carry closed/open
departure intervals and their actual suffix provenance. Reachability coverage
only schedules redundant suffixes later: those alternatives remain in memory
and can still join a prefix or improve the objective. ETA is never a constraint.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import heapq
import itertools
import math
import time

import numpy as np

from .departure_windows import Interval, _complement, _intersection, _merge
from .planning_control import checkpoint, search_progress
from .planning_limits import DEFAULT_MAX_TEMPORAL_LABELS, integer_setting, memory_pressure, process_peak_memory_mb
from .route_service import haversine_km
from .routing import NEIGHBOURS
from .temporal_routing import TemporalSearchExhausted
from .wind_time import frame_time, frame_boundaries, WIND_TIME_STEP_SECONDS as STEP
from .wrf_timeline import iso

MODEL = "TEN_MINUTE_BIDIRECTIONAL_INTERVAL_V1"


def shift(window, seconds):
    return Interval(window.start + seconds, window.end + seconds, window.left_closed, window.right_closed)


def cut_edge(a, b, distance_km):
    """A geodesic-length cut on the same displayed lon/lat segment."""
    total = haversine_km(a, b)
    if distance_km >= total - 1e-12:
        return b
    low, high = 0., 1.
    for _ in range(40):
        f = (low + high) / 2
        point = tuple(x + f * (y - x) for x, y in zip(a, b))
        if haversine_km(a, point) < distance_km:
            low = f
        else:
            high = f
    return tuple(x + (low + high) / 2 * (y - x) for x, y in zip(a, b))


class NativeGraph:
    """Native eight-neighbour graph with exact anchors and short virtual edges."""
    def __init__(self, weather, start, end, bbox, *, domain=None):
        self.weather, self.grid = weather, weather.grid
        self.start, self.goal = ("start",), ("goal",)
        self.anchors = {self.start: tuple(start), self.goal: tuple(end)}
        if tuple(start) == tuple(end):
            raise ValueError("起点和终点必须不同")
        for lon, lat in (start, end):
            if not min(self.grid.lons) <= lon <= max(self.grid.lons) or not min(self.grid.lats) <= lat <= max(self.grid.lats):
                raise ValueError("起点或终点超出风场空间覆盖范围")
        west, south, east, north = bbox
        self.rows = domain[0] if domain is not None else set(np.flatnonzero((self.grid.lats >= south) & (self.grid.lats <= north)).tolist())
        self.cols = domain[1] if domain is not None else set(np.flatnonzero((self.grid.lons >= west) & (self.grid.lons <= east)).tolist())
        if not self.rows or not self.cols:
            raise ValueError("风场空间覆盖不足")
        self.near = {}
        for anchor, point in self.anchors.items():
            rows = sorted(self.rows, key=lambda r: abs(self.grid.lats[r] - point[1]))[:2]
            cols = sorted(self.cols, key=lambda c: abs(self.grid.lons[c] - point[0]))[:2]
            self.near[anchor] = set(itertools.product(rows, cols))
        self.virtual, self.chains = {}, {}

    def point(self, node):
        if node in self.anchors:
            return self.anchors[node]
        if node in self.virtual:
            return self.virtual[node][0]
        return float(self.grid.lons[node[1]]), float(self.grid.lats[node[0]])

    def base_neighbours(self, node):
        if node in self.anchors:
            result = list(self.near[node])
            if self.near[self.start] & self.near[self.goal]:
                result.append(self.goal if node == self.start else self.start)
            return result
        r, c = node
        return [(r + dr, c + dc) for dr, dc in NEIGHBOURS if r + dr in self.rows and c + dc in self.cols] + [
            anchor for anchor, near in self.near.items() if node in near]

    def neighbours(self, node):
        if node in self.virtual:
            return self.virtual[node][1]
        result = []
        for other in self.base_neighbours(node):
            key = tuple(sorted((node, other), key=repr))
            if key not in self.chains:
                a, b = map(self.point, key)
                n = max(1, math.ceil(haversine_km(a, b) * 1000 / (STEP * self.weather.speed)))
                chain = [key[0]] + [("segment", key, index) for index in range(1, n)] + [key[1]]
                for index, virtual in enumerate(chain[1:-1], 1):
                    point = tuple(x + index / n * (y - x) for x, y in zip(a, b))
                    self.virtual[virtual] = (point, [chain[index - 1], chain[index + 1]])
                self.chains[key] = chain
            chain = self.chains[key]
            result.append(chain[1] if node == chain[0] else chain[-2])
        return result

    predecessors = neighbours


@dataclass(slots=True)
class Forward:
    node: object
    point: tuple
    stamp: float
    cost: float
    distance: float
    parent: object = None
    pending: object = None


@dataclass(slots=True)
class Backward:
    node: object
    window: Interval
    distance: float
    estimate: float
    parent: object = None
    serial: int = 0


class LocalKernel:
    """A* relaxation or LPA* g/rhs consistency on the lazy label graph."""
    def __init__(self, algorithm):
        self.algorithm, self.queue = algorithm, []
        self.g, self.rhs, self.records = {}, {}, {}
        self.serial = itertools.count()

    def offer(self, key, label, cost, heuristic):
        if cost >= self.rhs.get(key, math.inf):
            return False
        self.rhs[key], self.records[key] = cost, label
        if self.algorithm == "astar":
            self.g[key] = cost
        heapq.heappush(self.queue, (cost + heuristic, cost, next(self.serial), key))
        return True

    def pop(self):
        while self.queue:
            _, cost, _, key = heapq.heappop(self.queue)
            if cost != self.rhs[key]:
                continue
            if self.algorithm != "astar":
                if self.g.get(key, math.inf) == cost:
                    continue
                self.g[key] = cost
            return self.records[key]
        return None


class SegmentedPlanner:
    def __init__(self, weather, start, end, bbox, max_grid_cells=0, graph=None):
        self.weather, self.start, self.end = weather, tuple(start), tuple(end)
        if graph is None and len(weather.request.required_waypoints) > 2:
            from .ordered_waypoints import OrderedGraph
            graph = OrderedGraph(weather, weather.request.required_waypoints, bbox)
        self.graph = graph or NativeGraph(weather, start, end, bbox)
        self.departure = weather.request.departure_at.timestamp()
        self.finish = weather.dataset.frames[-1][0]
        self.stats = dict(forward_expanded=0, backward_expanded=0, forward_labels=0, backward_labels=0,
                          connection_attempts=0, complete_candidates=0, eta_calibrations=0,
                          reverse_windows_enabled=0, retained_suffix_alternatives=0,
                          max_forward_segment_seconds=0., first_feasible_seconds=None)
        self.stats.update(search_domain_bbox=list(bbox),
                          search_domain_grid_cells=(len(self.graph.rows) * len(self.graph.cols)
                                                    if isinstance(self.graph, NativeGraph) else None),
                          forward_expanded_bbox=None, backward_expanded_bbox=None)
        self.last_progress, self.began = -math.inf, time.monotonic()
        self.forward_at, self.backward_at = defaultdict(list), defaultdict(list)
        self.reverse_coverage = defaultdict(list)
        self.reverse_queue, self.alternative_queue = [], []
        self.serial = itertools.count()
        self.best, self.best_cost = None, math.inf
        self.best_stamp = None
        self.eta_hint = self.departure + self.forward_bound(self.graph.start, self.start) * 1000 / weather.speed
        self.tried, self.calibrated = set(), set()
        self.front_f, self.front_b = [], []
        self.wind_guidance=None

    @staticmethod
    def distance(a, b):
        return haversine_km(a, b)

    def track_expansion(self, side, point):
        key = f"{side}_expanded_bbox"
        previous = self.stats[key]
        lon, lat = point
        self.stats[key] = ([lon, lat, lon, lat] if previous is None else
                           [min(previous[0], lon), min(previous[1], lat),
                            max(previous[2], lon), max(previous[3], lat)])

    def prefix(self, label):
        result = []
        while label:
            result.append(label.point)
            label = label.parent
        return list(reversed(result))

    def suffix(self, label):
        result = []
        while label:
            result.append(self.graph.point(label.node))
            label = label.parent
        return result

    def _forward_key(self, label):
        # Exact same state/time with a cheaper prefix is the only cost dominance.
        return label.node, label.stamp, label.pending

    def forward_bound(self, node, point):
        return (self.graph.forward_bound(node, point) if hasattr(self.graph, 'forward_bound')
                else self.distance(point, self.end))

    def backward_bound(self, node, point):
        return (self.graph.backward_bound(node, point) if hasattr(self.graph, 'backward_bound')
                else self.distance(self.start, point))

    def compatible(self, forward, backward):
        return not hasattr(self.graph, 'compatible') or self.graph.compatible(forward.node, backward.node)

    def heuristic(self, point, stamp, target):
        # Ordering estimate only. Pruning still uses the proven distance bound.
        # A global time-expanded Dijkstra ordering spends its budget enumerating
        # loops in low-wind basins. The local kernel aims each 10-minute front
        # toward the opposite side, retaining all alternate labels for fallback.
        weights = self.weather.config.weights
        if (weights.epsilon_wind_exposure>0 and self.weather.loading_mode=='full'
                and isinstance(self.graph,NativeGraph) and len(self.graph.rows)*len(self.graph.cols)<=150000
                and integer_setting('ROUTE_PLAN_WIND_GUIDANCE',1)):
            if self.distance(point,target)<1e-9: return 0.
            from .wind_guidance import WindGuidance,MODEL as GUIDE_MODEL
            if self.wind_guidance is None:
                self.wind_guidance=WindGuidance(self.weather,self.start,self.graph.rows,self.graph.cols)
            value=max(self.distance(point,target)*weights.alpha_distance,self.wind_guidance.hint(point,target))
            self.stats.update(wind_guidance_model=GUIDE_MODEL,wind_guidance_seconds=self.wind_guidance.seconds)
            return value
        value = self.distance(point, target) * (weights.alpha_distance + .35 * weights.beta_wind)
        if self.algorithm == "wa_lpa_star":
            grid = self.weather.grid
            row, col = int(np.abs(grid.lats - point[1]).argmin()), int(np.abs(grid.lons - point[0]).argmin())
            vector = self.weather.vector(stamp, row, col)
            if vector:
                heading = math.atan2((target[0] - point[0]) * math.cos(math.radians(point[1])), target[1] - point[1])
                value += .05 * max(0., -vector[0] * math.sin(heading) - vector[1] * math.cos(heading))
        return value

    def _offer_f(self, label):
        remaining = self.forward_bound(label.node, label.point)
        limit = self.weather.request.max_route_distance_km
        if limit is not None and label.distance+remaining > limit+1e-9:
            return
        if label.stamp + remaining * 1000 / self.weather.speed > self.finish + 1e-6:
            return
        if label.cost + remaining * self.weather.config.weights.alpha_distance >= self.best_cost:
            return
        target = self.graph.forward_target(label.node) if hasattr(self.graph, 'forward_target') else self.end
        hint = self.heuristic(label.point, label.stamp, target) + max(0., remaining-self.distance(label.point,target))*self.weather.config.weights.alpha_distance
        if self.kernel.offer(self._forward_key(label), label, label.cost, hint):
            self.forward_at[label.node].append(label)
            self.stats["forward_labels"] += 1

    def _offer_b(self, label):
        point = self.graph.point(label.node)
        limit = self.weather.request.max_route_distance_km
        if limit is not None and label.distance+self.distance(self.start,point) > limit+1e-9:
            return
        earliest = self.departure + self.backward_bound(label.node, point) * 1000 / self.weather.speed
        domain = _intersection(label.window, Interval(earliest, self.finish))
        if domain is None:
            return
        label.window = domain
        # Coverage is a feasibility equivalence only, never cost dominance.
        novel = _complement(domain, self.reverse_coverage[label.node])
        serial = next(self.serial)
        label.serial = serial
        self.backward_at[label.node].append(label)
        priority = label.estimate + self.heuristic(point, (domain.start + domain.end) / 2, self.start)
        heapq.heappush(self.reverse_queue if novel else self.alternative_queue, (priority, serial, label))
        if novel:
            self.reverse_coverage[label.node] = _merge([*self.reverse_coverage[label.node], domain])
        else:
            self.stats["retained_suffix_alternatives"] += 1
        self.stats["backward_labels"] += 1
        for forward in self.forward_at.get(label.node, ()):
            if domain.contains(forward.stamp):
                self.connect(forward, label)

    def enable_arrivals(self):
        if self.next_arrival > self.finish:
            return False
        begin = self.next_arrival
        end = min(self.finish, frame_time(begin) + STEP)
        self._offer_b(Backward(self.graph.goal, Interval(begin, end, True, end == self.finish), 0., 0.))
        self.next_arrival = end if end < self.finish else self.finish + STEP
        self.stats["reverse_windows_enabled"] += 1
        return True

    def _candidate(self, points, via):
        if self.best is not None and self.weather.request.stop_on_first_feasible:
            return
        clean = [tuple(points[0])]
        clean.extend(tuple(point) for point in points[1:] if self.distance(clean[-1], point) > 1e-10)
        key = tuple(clean)
        if len(clean) < 2 or key in self.tried:
            return
        self.tried.add(key)
        cost = self.weather.route_cost(clean)
        if not math.isfinite(cost) or self.accept is None and cost >= self.best_cost - 1e-9:
            return
        distance = math.fsum(self.distance(a, b) for a, b in zip(clean, clean[1:]))
        candidate = dict(points=clean, distance_km=distance, objective_cost=cost, planner_type=self.algorithm,
                         search_phase=via, temporal_search_model=MODEL, expanded_nodes=self.expanded,
                         arrival_labels=self.stats["forward_labels"] + self.stats["backward_labels"],
                         planning_time_ms=(time.monotonic() - self.began) * 1000)
        # Independent full-voyage validation occurs before accepting or pruning.
        accepted = self.accept(candidate) if self.accept else candidate
        if accepted is None or accepted.get("analysis", {}).get("navigation_allowed", True) is False:
            return
        self.best = accepted
        self.best_cost = accepted.get("objective_cost", cost)
        distance = accepted.get("distance_km", distance)
        self.eta_hint = self.departure + distance * 1000 / self.weather.speed
        self.best_stamp = time.monotonic() if self.best_stamp is None else self.best_stamp
        self.stats["complete_candidates"] += 1
        if self.stats["first_feasible_seconds"] is None:
            self.stats["first_feasible_seconds"] = time.monotonic() - self.began

    @property
    def expanded(self):
        return self.stats["forward_expanded"] + self.stats["backward_expanded"]

    def connect(self, forward, backward):
        if not self.compatible(forward, backward):
            return
        if self.best is not None and self.weather.request.stop_on_first_feasible:
            return
        a, b = forward.point, self.graph.point(backward.node)
        limit = self.weather.request.max_route_distance_km
        if limit is not None and forward.distance+backward.distance+self.distance(a,b)>limit+1e-9:
            return
        duration = self.distance(a, b) * 1000 / self.weather.speed
        if forward.cost + (backward.distance + self.distance(a, b)) * self.weather.config.weights.alpha_distance >= self.best_cost:
            return
        if not backward.window.contains(forward.stamp + duration):
            return
        self.stats["connection_attempts"] += 1
        edge = self.weather.edge(a, b)
        if edge.allowed(forward.stamp):
            self.eta_hint = forward.stamp + duration + backward.distance * 1000 / self.weather.speed
            self._candidate([*self.prefix(forward), *self.suffix(backward)], "bidirectional_join")

    def calibrate(self, side, label):
        layer = int(label.distance * 1000 / self.weather.speed // 1800)
        if layer < 1:
            return
        score = ((label.cost if side == "forward" else label.estimate) + self.heuristic(
            label.point if side == "forward" else self.graph.point(label.node),
            label.stamp if side == "forward" else label.window.start,
            self.end if side == "forward" else self.start))
        key = (side, layer)
        previous = self.calibration_scores.get(key, [])
        if len(previous) >= 3 and score >= max(previous):
            return
        self.calibration_scores[key] = sorted([*previous, score])[:3]
        self.calibrated.add(key)
        self.stats["eta_calibrations"] += 1
        # Up to three frontier bridges, all tested at their actual passage time.
        fs = [label] if side == "forward" else self.front_f[-3:]
        bs = [label] if side == "backward" else self.front_b[-3:]
        pairs = sorted(itertools.product(fs, bs), key=lambda pair:
                       pair[0].cost + pair[1].estimate + self.distance(pair[0].point, self.graph.point(pair[1].node)))[:2]
        for f, b in pairs:
            self.connect(f, b)
        if side == "forward":
            self.eta_hint = label.stamp + self.forward_bound(label.node, label.point) * 1000 / self.weather.speed
            self.stats["connection_attempts"] += 1
            tail = self.graph.forward_tail(label.node) if hasattr(self.graph, 'forward_tail') else [self.end]
            self._candidate([*self.prefix(label), *tail], "thirty_minute_connection")
        else:
            point = self.graph.point(label.node)
            stamp = self.departure + self.backward_bound(label.node, point) * 1000 / self.weather.speed
            self.eta_hint = stamp + label.distance * 1000 / self.weather.speed
            if label.window.contains(stamp):
                self.stats["connection_attempts"] += 1
                head = self.graph.backward_head(label.node) if hasattr(self.graph, 'backward_head') else [self.start]
                self._candidate([*head, *self.suffix(label)], "thirty_minute_connection")

    def expand_forward(self):
        label = self.kernel.pop()
        if label is None:
            return False
        self.stats["forward_expanded"] += 1
        self.track_expansion("forward", label.point)
        self.front_f.append(label)
        self.front_f = self.front_f[-32:]
        self.weather.read_region(label.point, label.stamp)
        for backward in self.backward_at.get(label.node, ()):
            self.connect(label, backward)
        if label.node == self.graph.goal:
            self._candidate(self.prefix(label), "forward_goal")
            return True
        self.calibrate("forward", label)
        remaining = frame_time(label.stamp) + STEP - label.stamp
        for node in [label.pending] if label.pending is not None else self.graph.neighbours(label.node):
            end = self.graph.point(node)
            edge = self.weather.edge(label.point, end)
            if edge.duration < 1e-8:
                self._offer_f(Forward(node, end, label.stamp, label.cost, label.distance, label))
                continue
            pending = None
            if edge.duration > remaining + 1e-6:
                end = cut_edge(label.point, end, remaining * self.weather.speed / 1000)
                pending = node
                node = self.graph.boundary(end, pending) if hasattr(self.graph, 'boundary') else ("boundary", end)
                edge = self.weather.edge(label.point, end)
            if not edge.allowed(label.stamp):
                continue
            self.stats["max_forward_segment_seconds"] = max(self.stats["max_forward_segment_seconds"], edge.duration)
            cost = label.cost + self.weather.cost(edge, label.stamp)
            arrived = label.stamp + edge.duration
            boundary = frame_time(label.stamp) + STEP
            if abs(arrived - boundary) < 1e-6:
                arrived = boundary  # float arithmetic only; no waiting duration
            self._offer_f(Forward(node, end, arrived, cost,
                                  label.distance + edge.distance_km, label, pending))
        return True

    def expand_backward(self):
        queue = self.reverse_queue
        if not queue or self.stats["backward_expanded"] % 8 == 7 and self.alternative_queue:
            queue = self.alternative_queue
        if not queue:
            return False
        _, _, label = heapq.heappop(queue)
        self.stats["backward_expanded"] += 1
        self.front_b.append(label)
        self.front_b = self.front_b[-32:]
        point = self.graph.point(label.node)
        self.track_expansion("backward", point)
        self.weather.read_region(point, (label.window.start + label.window.end) / 2)
        for forward in self.forward_at.get(label.node, ()):
            self.connect(forward, label)
        if label.node == self.graph.start and label.window.contains(self.departure):
            self._candidate(self.suffix(label), "reverse_start")
        self.calibrate("backward", label)
        # Neighbours are predecessors on this symmetric spatial graph. Edge
        # direction remains predecessor -> current (never reversed wind cost).
        for node in self.graph.predecessors(label.node):
            before = self.graph.point(node)
            edge = self.weather.edge(before, point)
            if edge.duration < 1e-8:
                continue
            domain = shift(label.window, -edge.duration)
            domain = _intersection(domain, Interval(self.departure + self.backward_bound(node, before) * 1000 / self.weather.speed,
                                                    self.finish - edge.duration))
            if domain is None:
                continue
            for safe in self.weather.safe_departures(edge, domain):
                estimate = self.weather.cost(edge, (safe.start + safe.end) / 2)
                self._offer_b(Backward(node, safe, label.distance + edge.distance_km,
                                       label.estimate + estimate, label))
        return True

    def progress(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_progress < 1.:
            return
        self.last_progress = now
        def representatives(labels, side):
            result, seen = [], set()
            ordered = sorted(labels, key=lambda item: (self.distance(item.point if side == "forward" else self.graph.point(item.node),
                                                                    self.end if side == "forward" else self.start)))
            for label in ordered:
                path = self.prefix(label) if side == "forward" else self.suffix(label)
                if len(path) < 2:
                    continue
                # Display dedup does not prune the search state.
                cells = tuple((int(np.abs(self.weather.grid.lats - p[1]).argmin()),
                               int(np.abs(self.weather.grid.lons - p[0]).argmin())) for p in path)
                if cells in seen:
                    continue
                seen.add(cells)
                result.append({"direction": side, "points": path,
                               "at": iso(label.stamp) if side == "forward" else None,
                               "time_window": {"start": iso(label.window.start), "end": iso(label.window.end)} if side == "backward" else None})
                if len(result) == 3:
                    break
            return result
        search_progress({"model": MODEL, "forward": representatives(self.front_f, "forward"),
                         "backward": representatives(self.front_b, "backward"), "eta_hint": iso(self.eta_hint),
                         "preview_context": {"dataset_id": self.weather.dataset.dataset_id,
                             "source": self.weather.dataset.source, "level": self.weather.request.level,
                             "departure_at": iso(self.departure), "average_ground_speed_mps": self.weather.speed,
                             "frame_times": [iso(stamp) for stamp, _ in self.weather.dataset.frames],
                             "wind_time_model": self.weather.dataset.time_model},
                         "statistics": self.diagnostics()})

    def diagnostics(self):
        return {**self.stats, "arrival_labels": self.stats["forward_labels"] + self.stats["backward_labels"],
                "process_peak_memory_mb": process_peak_memory_mb(),
                "expanded_nodes": self.expanded, "elapsed_seconds": time.monotonic() - self.began,
                "weather": self.weather.diagnostics(), "temporal_search_model": MODEL}

    def release_search_state(self):
        """Drop task trees explicitly; do not wait for cyclic garbage collection.

        Matching generators retain the coordinator, and cached edges retain
        their weather owner. Diagnostics/results are kept independently.
        """
        began=time.monotonic()
        for generator in getattr(self,'joins',()):
            generator.close()
        for name in ('forward_at','backward_at','reverse_coverage','reverse_queue','alternative_queue',
                     'front_f','front_b','tried','calibrated','labels','buckets','f_times','b_windows',
                     'joins','pending','joined','ids','delta','candidates','calibration_scores'):
            value=getattr(self,name,None)
            if value is not None: value.clear()
        kernel=getattr(self,'kernel',None)
        if kernel is not None:
            for name in ('queue','g','rhs','records'): getattr(kernel,name).clear()
        graph=self.graph
        if isinstance(graph,NativeGraph):
            anchors={node:graph.anchors[node] for node in (graph.start,graph.goal)}
            graph.anchors.clear();graph.anchors.update(anchors)
            graph.virtual.clear();graph.chains.clear()
        elif hasattr(graph, 'clear'):
            graph.clear()
        self.weather.edges.clear()
        self.weather.vectors.clear()
        self.wind_guidance=None
        self.future=None
        self.accept=None
        self.stats['search_state_release_seconds']=time.monotonic()-began

    def search(self, algorithm="wa_lpa_star", accept=None):
        self.algorithm = "astar" if algorithm == "a_star" else algorithm
        self.kernel, self.accept = LocalKernel(self.algorithm), accept
        self.next_arrival = self.eta_hint
        self.calibration_scores = {}
        self._offer_f(Forward(self.graph.start, self.start, self.departure, 0., 0.))
        self.enable_arrivals()
        self.enable_arrivals()
        self.enable_arrivals()
        next_enable = time.monotonic() + .5
        for points in self.weather.request.seed_routes:
            self._candidate(points,'cached_seed')
            if self.best is not None and self.weather.request.stop_on_first_feasible:
                break
        self.progress(force=True)
        limit = integer_setting("ROUTE_PLAN_MAX_TEMPORAL_LABELS", DEFAULT_MAX_TEMPORAL_LABELS)
        # Production keeps the enclosing 7200s budget. A separate, opt-in
        # comparison budget is used by isolated benchmarks, never silently
        # imposed on normal searches.
        improvement = integer_setting("ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS", 0)
        while self.kernel.queue or self.reverse_queue or self.alternative_queue or self.next_arrival <= self.finish:
            if self.best is not None and self.weather.request.stop_on_first_feasible:
                self.stats["stop_reason"] = "first_feasible"
                break
            checkpoint()
            if self.expanded % 128 == 0:
                pressure = memory_pressure()
                if pressure:
                    raise TemporalSearchExhausted("搜索达到可用内存资源上限；保留已验证结果", {**self.diagnostics(), **pressure})
            if limit and self.stats["forward_labels"] + self.stats["backward_labels"] >= limit:
                raise TemporalSearchExhausted("搜索达到时间标签资源上限；保留已验证结果", {**self.diagnostics(), "limit_type": "arrival_labels"})
            # Activate another arrival frame without preloading its weather.
            if time.monotonic() >= next_enable or not self.reverse_queue:
                self.enable_arrivals()
                next_enable = time.monotonic() + .5
            moved_f = self.expand_forward()
            if self.best is not None and self.weather.request.stop_on_first_feasible:
                self.stats["stop_reason"] = "first_feasible"
                break
            moved_b = self.expand_backward()
            if self.best is not None and self.weather.request.stop_on_first_feasible:
                self.stats["stop_reason"] = "first_feasible"
                break
            self.progress()
            if self.best is not None and self.best_cost <= self.forward_bound(self.graph.start, self.start) * self.weather.config.weights.alpha_distance + 1e-8:
                self.stats["stop_reason"] = "objective_lower_bound_reached"
                break
            if self.best is not None and not self.kernel.queue:
                # Every forward local state capable of improving the incumbent
                # has been processed. No need to enumerate unrelated future
                # reverse windows after the forward frontier has closed.
                self.stats["stop_reason"] = "forward_frontier_exhausted"
                break
            if improvement and self.best is not None and time.monotonic() - self.best_stamp >= improvement:
                self.stats["stop_reason"] = "bounded_candidate_comparison"
                break
            if not moved_f and not moved_b and self.next_arrival > self.finish:
                break
        self.progress()
        if self.best is None:
            raise TemporalSearchExhausted("当前时空图中未找到可行航线", {**self.diagnostics(), "limit_type": "exhausted_graph"})
        self.best.update(self.diagnostics(), planning_time_ms=(time.monotonic() - self.began) * 1000)
        return self.best
