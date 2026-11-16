"""Coordinator for persistent forward/reverse search processes.

Only the coordinator publishes, and only its validation thread accepts a
complete, retimed, independently checked route. Frontier geometry is separate.
"""
from bisect import bisect_left, bisect_right, insort
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import math
from multiprocessing.connection import wait
import os
import time
from uuid import uuid4
from threading import Event

import numpy as np

from .direction_pool import direction_pool, direction_lease
from .forecast_transport import describe_dataset
from .planning_control import checkpoint, current_control, planning_scope, PlanningTimedOut
from .planning_limits import DEFAULT_MAX_TEMPORAL_LABELS, integer_setting, memory_pressure
from .route_refinement import RouteRefiner, clean_points, shape_quality
from .segmented_routing import SegmentedPlanner, Forward, Backward, MODEL
from .temporal_routing import TemporalSearchExhausted


class ParallelPlanner(SegmentedPlanner):
    def __init__(self, weather, start, end, bbox, max_grid_cells=0):
        super().__init__(weather, start, end, bbox, max_grid_cells)
        self.bbox = bbox
        self.labels = {"forward": {}, "backward": {}}
        self.buckets = {"forward": defaultdict(set), "backward": defaultdict(set)}
        self.f_times, self.b_windows = defaultdict(list), defaultdict(list)
        self.joins, self.pending = deque(), deque()
        self.joined = set()
        self.worker_stats = {}
        self.intervals = {"forward": [], "backward": []}
        self.ids = {}
        self.future = None
        self.initial_connection_tried = False
        self.validation_turn = 0

    def cell(self, point):
        return int(np.abs(self.weather.grid.lats - point[1]).argmin()), int(np.abs(self.weather.grid.lons - point[0]).argmin())

    def near_nodes(self, point, side):
        grid, radius = self.weather.grid, self.weather.speed * 600
        row, col = self.cell(point)
        dr = math.ceil(radius / max(1., abs(float(grid.lats[1] - grid.lats[0])) * 110574)) + 2
        dc = math.ceil(radius / max(1., abs(float(grid.lons[1] - grid.lons[0])) * 111320 * math.cos(math.radians(point[1])))) + 2
        nodes = []
        for r in range(max(0, row - dr), min(len(grid.lats), row + dr + 1)):
            for c in range(max(0, col - dc), min(len(grid.lons), col + dc + 1)):
                nodes.extend(tuple(self.buckets[side].get((r, c), ())))
        return nodes

    def matching(self, label, side):
        if side == "forward":
            for node in self.near_nodes(label.point, "backward"):
                point = self.graph.point(node)
                duration = self.distance(label.point, point) * 1000 / self.weather.speed
                if duration > 600 + 1e-6:
                    continue
                stamp = label.stamp + duration
                for backward in tuple(self.b_windows.get((node, int(stamp // 600)), ())):
                    if backward.window.contains(stamp):
                        yield label, backward
        else:
            point = self.graph.point(label.node)
            for node in self.near_nodes(point, "forward"):
                duration = self.distance(self.graph.point(node), point) * 1000 / self.weather.speed
                if duration > 600 + 1e-6:
                    continue
                values = self.f_times[node]
                lo = bisect_left(values, (label.window.start - duration, -1))
                hi = bisect_right(values, (label.window.end - duration, math.inf))
                for _, _, forward in values[lo:hi]:
                    if label.window.contains(forward.stamp + duration):
                        yield forward, label

    def ingest(self, value):
        side = value["role"]
        self.worker_stats[side] = {key: value[key] for key in ("pid", "weather", "peak_memory_mb")}
        self.worker_stats[side].update({key:value['statistics'][key] for key in
            ('wind_guidance_model','wind_guidance_seconds') if key in value['statistics']})
        self.intervals[side].append((value["start"], value["end"]))
        self.intervals[side] = self.intervals[side][-64:]
        stats = value["statistics"]
        for key in (f"{side}_expanded", f"{side}_labels"):
            self.stats[key] = stats[key]
        self.stats[f"{side}_expanded_bbox"] = stats[f"{side}_expanded_bbox"]
        self.stats["max_forward_segment_seconds"] = max(self.stats["max_forward_segment_seconds"], stats["max_forward_segment_seconds"])
        self.stats["reverse_windows_enabled"] = max(self.stats["reverse_windows_enabled"], stats["reverse_windows_enabled"])
        self.stats["retained_suffix_alternatives"] = max(self.stats["retained_suffix_alternatives"], stats["retained_suffix_alternatives"])
        self.stats["eta_calibrations"] = max(self.stats["eta_calibrations"], stats["eta_calibrations"])
        for record in value["records"]:
            parent = self.labels[side].get(record["parent"])
            # Stable virtual geometry is metadata, not a weather array.
            self.graph.anchors[record["node"]] = record["point"]
            if side == "forward":
                label = Forward(record["node"], record["point"], record["stamp"], record["cost"], record["distance"], parent, record["pending"])
                self.forward_at[label.node].append(label)
                insort(self.f_times[label.node], (label.stamp, record["id"], label))
                self.front_f.append(label)
                self.front_f = self.front_f[-32:]
            else:
                label = Backward(record["node"], record["window"], record["distance"], record["estimate"], parent, record["serial"])
                self.backward_at[label.node].append(label)
                for frame in range(int(label.window.start // 600), int(label.window.end // 600) + 1):
                    self.b_windows[label.node, frame].append(label)
                self.front_b.append(label)
                self.front_b = self.front_b[-32:]
            self.labels[side][record["id"]] = label
            self.ids[id(label)] = record["id"]
            self.buckets[side][self.cell(record["point"])].add(label.node)
            self.joins.append(self.matching(label, side))
        for candidate in value["candidates"]:
            self._candidate(candidate["points"], candidate["search_phase"])
        if not self.initial_connection_tried and self.expanded:
            self.initial_connection_tried = True
            self._candidate(self.weather.request.required_waypoints or [self.start, self.end], "early_temporal_connection")

    def _candidate(self, points, via):
        points = clean_points(points)
        key = tuple(points)
        if len(points) < 2 or key in self.tried:
            return
        self.tried.add(key)
        self.pending.append(dict(points=points, planner_type=self.algorithm, search_phase=via,
                                 temporal_search_model=MODEL, expanded_nodes=self.expanded,
                                 arrival_labels=self.stats["forward_labels"] + self.stats["backward_labels"],
                                 planning_time_ms=(time.monotonic() - self.began) * 1000))

    def connect(self, forward, backward):
        if not self.compatible(forward, backward):
            return
        a, b = forward.point, self.graph.point(backward.node)
        limit = self.weather.request.max_route_distance_km
        if limit is not None and forward.distance+backward.distance+self.distance(a,b)>limit+1e-9:
            return
        duration = self.distance(a, b) * 1000 / self.weather.speed
        if not backward.window.contains(forward.stamp + duration):
            return
        if forward.cost + (backward.distance + self.distance(a, b)) * self.weather.config.weights.alpha_distance >= self.best_cost:
            return
        self.stats["connection_attempts"] += 1
        # Full weather checking belongs to the validation thread; receiving
        # frontier batches must never wait for a cold source-variable decode.
        before = len(self.pending)
        self._candidate([*self.prefix(forward), *self.suffix(backward)], "bidirectional_join")
        if len(self.pending) > before:
            self.pending[-1]['connection_check'] = {'points': [a,b], 'departure': forward.stamp}

    def drain_joins(self):
        deadline = time.monotonic() + .02
        while self.joins and len(self.pending) < 128 and time.monotonic() < deadline:
            checkpoint()
            try:
                forward, backward = next(self.joins[0])
            except StopIteration:
                self.joins.popleft()
                continue
            key = self.ids[id(forward)], self.ids[id(backward)]
            if key in self.joined:
                continue
            self.joined.add(key)
            self.connect(forward, backward)

    def diagnostics(self):
        result = super().diagnostics()
        overlap = sum(max(0., min(b, d) - max(a, c)) for a, b in self.intervals["forward"] for c, d in self.intervals["backward"])
        return {**result, "execution_mode": "parallel_processes", "direction_workers": self.worker_stats,
                "observed_parallel_overlap_seconds": overlap,
                "pending_connections": len(self.joins), "pending_validations": len(self.pending)}

    def _accept_default(self, candidate):
        points, cost, refinement = RouteRefiner(self.weather, candidate["points"]).finish()
        if not math.isfinite(cost):
            return None
        from .route_jobs import candidate_score
        result = {**candidate, "points": points, "objective_cost": cost,
                  "distance_km": sum(self.distance(a, b) for a, b in zip(points, points[1:])),
                  "shape_quality": shape_quality(points), "refinement": refinement,
                  "analysis": {"navigation_allowed": True}}
        return result if self.best is None or candidate_score(result) < candidate_score(self.best) else None

    def search(self, algorithm="wa_lpa_star", accept=None):
        try:
            with direction_lease():
                return self._search(algorithm, accept)
        finally:
            self.release_search_state()

    def _search(self, algorithm="wa_lpa_star", accept=None):
        from .route_jobs import candidate_score
        self.algorithm = "astar" if algorithm == "a_star" else algorithm
        token, pool = uuid4().hex, direction_pool()
        before = time.monotonic()
        dataset_spec = describe_dataset(self.weather.dataset)
        self.stats["transport_prepare_seconds"] = time.monotonic() - before
        workers = pool.acquire()
        done, sent = set(), set()
        validator = ThreadPoolExecutor(max_workers=1, thread_name_prefix="route-validation")
        control = current_control()
        deadline = control.deadline if control else time.monotonic() + 7200
        validation_stop = Event()
        class ValidationCancel:
            def is_set(self):
                return validation_stop.is_set() or bool(control and control.cancel.is_set())
        def validate(value):
            with planning_scope(deadline, ValidationCancel()):
                connection = value.get('connection_check')
                if connection and not self.weather.edge(*connection['points']).allowed(connection['departure']):
                    return None
                value = {key:item for key,item in value.items() if key != 'connection_check'}
                return (accept or self._accept_default)(value)
        compare_seconds = integer_setting("ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS", 0)
        frontier_closed = False
        try:
            for role, item in workers.items():
                item["connection"].send(dict(type="start", token=token, dataset=dataset_spec,
                    request=self.weather.request.model_dump(mode="json"), bbox=self.bbox,
                    algorithm=self.algorithm, deadline=deadline, weather_loading_mode=self.weather.loading_mode))
                sent.add(role)
            for points in self.weather.request.seed_routes:
                self._candidate(points,'cached_seed')
            while True:
                checkpoint()
                limit = integer_setting("ROUTE_PLAN_MAX_TEMPORAL_LABELS", DEFAULT_MAX_TEMPORAL_LABELS)
                if limit and self.stats["forward_labels"] + self.stats["backward_labels"] >= limit:
                    raise TemporalSearchExhausted("搜索达到时间标签资源上限；保留已验证结果", {**self.diagnostics(), "limit_type": "arrival_labels"})
                pressure = memory_pressure()
                if pressure:
                    raise TemporalSearchExhausted("搜索达到可用内存资源上限；保留已验证结果", {**self.diagnostics(), **pressure})
                channels = [item["connection"] for role, item in workers.items() if role not in done]
                # Backpressure bounds candidate backlog without discarding search labels.
                ready = wait(channels, timeout=.005) if channels and len(self.pending) < 128 else []
                for channel in ready:
                    try:
                        value = channel.recv()
                    except (EOFError, OSError) as exc:
                        raise RuntimeError("双向搜索工作进程异常退出") from exc
                    if value.get("token") != token:
                        continue
                    if value.get('dataset_id') != self.weather.dataset.dataset_id:
                        raise RuntimeError('双向工作进程返回了不同预报版本')
                    if value["type"] == "batch":
                        if not frontier_closed:
                            self.ingest(value)
                    elif value["type"] == "done":
                        done.add(value["role"])
                    elif value["type"] == "error":
                        if frontier_closed and value["error"] == "PlanningCancelled":
                            continue
                        if value["error"] in {"MemoryError", "TemporalSearchExhausted"}:
                            raise TemporalSearchExhausted(value["message"], {**self.diagnostics(),
                                "limit_type": "available_memory", **value.get("diagnostics", {})})
                        if value['error']=='PlanningTimedOut':
                            raise PlanningTimedOut()
                        raise RuntimeError(value["message"] or value["error"])
                self.drain_joins()
                if self.future and self.future.done():
                    result = self.future.result()
                    self.future = None
                    if result and (self.best is None or candidate_score(result) < candidate_score(self.best)):
                        self.best, self.best_cost = result, result["objective_cost"]
                        self.eta_hint = self.departure + result['distance_km'] * 1000 / self.weather.speed
                        self.best_stamp = self.best_stamp or time.monotonic()
                        self.stats["complete_candidates"] += 1
                        self.stats["first_feasible_seconds"] = self.stats["first_feasible_seconds"] or time.monotonic() - self.began
                        if self.weather.request.stop_on_first_feasible:
                            self.stats["stop_reason"] = "first_feasible"
                            break
                        for role, item in workers.items():
                            if role not in done:
                                item["connection"].send(dict(type="bound", token=token, value=self.best_cost))
                if self.future is None and self.pending:
                    self.validation_turn += 1
                    # Verified goal arrivals and local joins get first access
                    # to validation; a fair oldest-first slot retains remote
                    # proposals rather than letting them block the first route.
                    priorities = {'cached_seed':-1,'forward_goal':0, 'reverse_start':0, 'bidirectional_join':1,
                                  'early_temporal_connection':1, 'thirty_minute_connection':2}
                    index = (0 if self.validation_turn % 8 == 0 else min(range(len(self.pending)),
                        key=lambda i: priorities.get(self.pending[i]['search_phase'], 2)))
                    value = self.pending[index]
                    del self.pending[index]
                    context = copy_context()
                    self.future = validator.submit(context.run, validate, value)
                self.progress()
                if self.best and self.best_cost <= self.forward_bound(self.graph.start, self.start) * self.weather.config.weights.alpha_distance + 1e-8:
                    self.stats["stop_reason"] = "objective_lower_bound_reached"
                    break
                if self.best and compare_seconds and time.monotonic() - self.best_stamp >= compare_seconds:
                    self.stats["stop_reason"] = "bounded_candidate_comparison"
                    break
                if "forward" in done and not self.future and not self.pending and not self.joins and self.best:
                    self.stats["stop_reason"] = "forward_frontier_exhausted"
                    break
                if "forward" in done and self.best and not frontier_closed:
                    frontier_closed = True
                    pool.cancel.set()
                if len(done) == 2 and not self.future and not self.pending and not self.joins:
                    self.stats["stop_reason"] = "exhausted_graph"
                    break
        finally:
            validation_stop.set()
            self.stats.update(pool.release(token, done | (set(workers) - sent)))
            # Validation observes validation_stop at all geometry checkpoints.
            # Join it before releasing task-owned arrays and search indexes.
            validator.shutdown(wait=True, cancel_futures=True)
        if self.best is None:
            raise TemporalSearchExhausted("当前时空图中未找到可行航线", {**self.diagnostics(), "limit_type": "exhausted_graph"})
        self.best.update(self.diagnostics(), planning_time_ms=(time.monotonic() - self.began) * 1000)
        return self.best
