"""Full or tiled native weather access with exact held-frame edge tests."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import os
from threading import RLock
from time import monotonic

import numpy as np

from .cost_evaluator import cost_config_from_thresholds
from .departure_windows import Interval, _cell_visits, _complement, _merge
from .planning_control import checkpoint
from .planning_limits import memory_pressure
from .planning_objective import edge_objective
from .grid_coverage import occupancy_spans, shear_contacts, boundary_contacts, SPATIAL_RISK_MODEL
from .route_service import haversine_km
from .wind_time import frame_time, frame_boundaries, WIND_TIME_STEP_SECONDS as STEP
from .wrf_timeline import bracket

_BLOCKS, _BLOCK_LOCK = OrderedDict(), RLock()
BLOCK_SIZE = 16  # Cache tile size, never a limit on reachable cells.


def preserves_waypoints(points, anchors):
    if not anchors:
        return True
    index = 0
    for point in points:
        if tuple(point[:2]) == tuple(anchors[index]):
            index += 1
            if index == len(anchors):
                return True
    return False


@dataclass
class LocalEdge:
    weather: object
    points: tuple
    distance_km: float
    duration: float
    visits: tuple
    spans: tuple
    contacts: tuple
    shear: tuple

    def allowed(self, departure):
        return self.weather.allowed(self, departure)


class LocalWeather:
    def __init__(self, dataset, request, config, *, loading_mode=None):
        if not dataset.frames:
            raise ValueError(dataset.error or "缺少小时风场")
        self.dataset, self.request, self.grid = dataset, request, dataset.frames[0][1]
        self.speed = request.average_ground_speed_mps
        if request.minimize_distance_under_peak:
            config = {'weights': dict(alpha_distance=1., beta_wind=0., gamma_headwind=0.,
                                     delta_crosswind=0., epsilon_wind_exposure=0.)}
        self.config = cost_config_from_thresholds(request.thresholds, config)
        settings = request.wind_shear
        self.shear_limit = (settings.horizontal.hard_delta_wind_vector_ms if settings.enabled and
                            settings.horizontal.enabled and settings.horizontal.hard_constraint_enabled else None)
        self.edges, self.vectors = OrderedDict(), OrderedDict()
        self.stats = dict(local_block_cells=0, local_block_reads=0, block_cache_hits=0,
                          local_queries=0, maximum_region_cells=0, data_failures=0)
        self.stats["missing_cell_vectors"] = 0
        self.failures = set()
        self.region_cache = set()
        self.loading_mode = loading_mode or os.getenv("ROUTE_PLAN_WEATHER_LOADING", "full")
        if self.loading_mode not in {"full", "local"}:
            raise ValueError("ROUTE_PLAN_WEATHER_LOADING 仅支持 full 或 local")
        self.full_fields = {}
        self.stats.update(full_field_spatial_cells=0, full_field_vector_values=0, full_field_bytes=0,
                          full_field_load_seconds=0., full_field_hours_loaded=0, full_field_queries=0)
        if self.loading_mode == "full":
            self._load_full_fields()

    def _load_full_fields(self):
        """Materialize every native U/V cell at this height for all pinned hours.

        Keep the immutable source descriptors for subprocess transport and
        replay. These private, read-only arrays are task-owned, not map crops.
        """
        began = monotonic()
        for _, grid in self.dataset.frames:
            checkpoint()
            if memory_pressure():
                raise MemoryError("完整风场加载达到可用内存余量限制")
            shape = (len(grid.lats), len(grid.lons))
            try:
                u = np.array(grid.u, dtype=float, copy=True)
                checkpoint()
                v = np.array(grid.v, dtype=float, copy=True)
                if u.shape != shape or v.shape != shape:
                    raise ValueError("预报变量维度不一致")
            except (OSError, ValueError, KeyError, EOFError) as exc:
                self.failures.add(f"{grid.valid_time}: {exc}")
                self.stats["data_failures"] += 1
                u, v = np.full(shape, np.nan), np.full(shape, np.nan)
            u.flags.writeable = v.flags.writeable = False
            self.full_fields[grid.valid_time] = (u, v)
            self.stats["full_field_hours_loaded"] += 1
            self.stats["full_field_spatial_cells"] += u.size
            self.stats["full_field_vector_values"] += u.size + v.size
            self.stats["full_field_bytes"] += u.nbytes + v.nbytes
        self.stats["full_field_load_seconds"] = monotonic() - began
        checkpoint()

    def _block(self, stamp, grid, row, col):
        tr, tc = row // BLOCK_SIZE, col // BLOCK_SIZE
        key = (self.dataset.dataset_id, stamp, self.dataset.level, tr, tc)
        with _BLOCK_LOCK:
            if key in _BLOCKS:
                _BLOCKS.move_to_end(key)
                self.stats["block_cache_hits"] += 1
                return _BLOCKS[key]
            r, c = tr * BLOCK_SIZE, tc * BLOCK_SIZE
            checkpoint()
            try:
                u = np.asarray(grid.u[r:r + BLOCK_SIZE, c:c + BLOCK_SIZE]).copy()
                v = np.asarray(grid.v[r:r + BLOCK_SIZE, c:c + BLOCK_SIZE]).copy()
            except (OSError, ValueError, KeyError, EOFError) as exc:
                self.failures.add(str(exc))
                self.stats["data_failures"] += 1
                shape = (min(BLOCK_SIZE, len(grid.lats) - r), min(BLOCK_SIZE, len(grid.lons) - c))
                u, v = np.full(shape, np.nan), np.full(shape, np.nan)
            self.stats["local_block_reads"] += 1
            self.stats["local_block_cells"] += int(u.size)
            _BLOCKS[key] = (u, v)
            while len(_BLOCKS) > 1024:
                _BLOCKS.popitem(last=False)
            return u, v

    def vector(self, stamp, row, col):
        if stamp < self.dataset.frames[0][0] or stamp > self.dataset.frames[-1][0]:
            return None
        key = (frame_time(stamp), row, col)
        if key in self.vectors:
            self.vectors.move_to_end(key)
            return self.vectors[key]
        pair = bracket(self.dataset, stamp)
        if pair is None:
            self.stats["missing_cell_vectors"] += 1
            return None
        left, right, alpha = pair
        values = []
        for grid in (left, right) if alpha else (left,):
            if self.loading_mode == "full":
                u, v = self.full_fields[grid.valid_time]
                values.append((float(u[row, col]), float(v[row, col])))
            else:
                u, v = self._block(grid.valid_time, grid, row, col)
                values.append((float(u[row % BLOCK_SIZE, col % BLOCK_SIZE]), float(v[row % BLOCK_SIZE, col % BLOCK_SIZE])))
        result = (tuple(a + alpha * (b - a) for a, b in zip(*values)) if alpha else values[0])
        if not all(map(math.isfinite, result)):
            result = None
            self.stats["missing_cell_vectors"] += 1
        self.vectors[key] = result
        if len(self.vectors) > 250000:
            self.vectors.popitem(last=False)
        return result

    def read_region(self, point, stamp):
        """Reachable 600*v disk bounding box + full one-cell shear halo.

        Large boxes are read in tiles; no source-grid thinning. Cache block
        alignment may read a little more than the requested region.
        """
        if self.loading_mode == "full":
            self.stats["maximum_region_cells"] = len(self.grid.lats) * len(self.grid.lons)
            self.stats["full_field_queries"] += 1
            return
        radius = STEP * self.speed
        dy = radius / 110574.
        dx = radius / max(1., 111320. * math.cos(math.radians(point[1])))
        rows = np.flatnonzero((self.grid.lats >= point[1] - dy) & (self.grid.lats <= point[1] + dy))
        cols = np.flatnonzero((self.grid.lons >= point[0] - dx) & (self.grid.lons <= point[0] + dx))
        rr = int(np.abs(self.grid.lats - point[1]).argmin())
        cc = int(np.abs(self.grid.lons - point[0]).argmin())
        r0, r1 = max(0, (int(rows.min()) if rows.size else rr) - 1), min(len(self.grid.lats), (int(rows.max()) if rows.size else rr) + 2)
        c0, c1 = max(0, (int(cols.min()) if cols.size else cc) - 1), min(len(self.grid.lons), (int(cols.max()) if cols.size else cc) + 2)
        key = (frame_time(stamp), r0 // BLOCK_SIZE, (r1 - 1) // BLOCK_SIZE, c0 // BLOCK_SIZE, (c1 - 1) // BLOCK_SIZE)
        self.stats["maximum_region_cells"] = max(self.stats["maximum_region_cells"], (r1 - r0) * (c1 - c0))
        if key in self.region_cache:
            return
        pair = bracket(self.dataset, stamp)
        if pair is None:
            return
        for grid in (pair[0], pair[1]) if pair[2] else (pair[0],):
            for row in range(r0 // BLOCK_SIZE * BLOCK_SIZE, r1, BLOCK_SIZE):
                for col in range(c0 // BLOCK_SIZE * BLOCK_SIZE, c1, BLOCK_SIZE):
                    self._block(grid.valid_time, grid, row, col)
        self.region_cache.add(key)
        self.stats["local_queries"] += 1

    def edge(self, a, b):
        a, b = tuple(a[:2]), tuple(b[:2])
        key = a, b
        if key not in self.edges:
            distance = haversine_km(a, b)
            visits = tuple(_cell_visits([a, b], self.grid, self.speed))
            self.edges[key] = LocalEdge(self, key, distance, distance * 1000 / self.speed, visits,
                occupancy_spans(visits), boundary_contacts([a,b], self.grid, self.speed), shear_contacts(visits))
            if len(self.edges) > 60000:
                self.edges.popitem(last=False)
        return self.edges[key]

    def _danger(self, stamp, cell, other=None):
        vector = self.vector(stamp, *cell)
        if vector is None:
            return True
        if other is not None:
            right = self.vector(stamp, *other)
            return right is None or math.hypot(vector[0] - right[0], vector[1] - right[1]) >= self.shear_limit
        magnitude = math.hypot(*vector)
        cap = self.request.peak_wind_limit_mps
        return magnitude >= self.request.thresholds.danger or (cap is not None and (
            magnitude >= cap if self.request.peak_wind_limit_exclusive else magnitude > cap))

    def _hazards(self, cell, domain, other=None):
        start, end = self.dataset.frames[0][0], self.dataset.frames[-1][0]
        result = []
        if domain.start < start:
            result.append(Interval(-math.inf, start, True, False))
        if domain.end > end:
            result.append(Interval(end, math.inf, False, True))
        begin, finish = max(start, domain.start), min(end, domain.end)
        if begin <= finish:
            frame = frame_time(begin)
            while frame <= finish:
                if self._danger(frame, cell, other):
                    result.append(Interval(frame, min(frame + STEP, end), True, frame + STEP > end))
                frame += STEP
            # Coverage end is an actual known vector, with no forecast extension.
            if finish == end and self._danger(end, cell, other):
                result.append(Interval(end, end))
        return result

    def safe_departures(self, edge, domain):
        """Exact interval propagation; endpoints alone are never a proof."""
        forbidden = []
        for visit in edge.visits:
            for item in self._hazards((visit.row, visit.col),
                                      Interval(domain.start + visit.enter, domain.end + visit.leave)):
                forbidden.append(Interval(item.start - visit.leave, item.end - visit.enter,
                                          item.left_closed, item.right_closed))
        if self.shear_limit is not None:
            for ca, cb, contact in edge.shear:
                for item in self._hazards(ca, Interval(domain.start + contact.enter, domain.end + contact.leave), cb):
                    forbidden.append(Interval(item.start - contact.leave, item.end - contact.enter,
                                              item.left_closed, item.right_closed))
        return _complement(domain, forbidden)

    def allowed(self, edge, departure):
        # Avoid constructing interval unions for exact forward queries.
        if departure < self.dataset.frames[0][0] or departure + edge.duration > self.dataset.frames[-1][0]:
            return False
        for visit in edge.visits:
            cell = (visit.row, visit.col)
            begin, end = departure + visit.enter, departure + visit.leave
            for stamp in (begin, *frame_boundaries(begin, end), end):
                if self._danger(stamp, cell):
                    return False
        if self.shear_limit is not None:
            for ca, cb, contact in edge.shear:
                begin, end = departure + contact.enter, departure + contact.leave
                if any(self._danger(stamp, ca, cb) for stamp in (begin, *frame_boundaries(begin,end), end)):
                    return False
        return True

    def cost(self, edge, departure):
        if self.request.minimize_distance_under_peak:
            return edge.distance_km
        return edge_objective(edge, departure, self.vector, self.config)

    def route_cost(self, points, *, require_allowed=True):
        if not preserves_waypoints(points, self.request.required_waypoints):
            return math.inf
        stamp, costs = self.request.departure_at.timestamp(), []
        for a, b in zip(points, points[1:]):
            edge = self.edge(a, b)
            if require_allowed and not edge.allowed(stamp):
                return math.inf
            costs.append(self.cost(edge, stamp))
            stamp += edge.duration
        return math.fsum(costs)

    def contact_peak(self, points, departure=None):
        """Exact held-frame peak, including zero-duration closed-cell contacts."""
        stamp = self.request.departure_at.timestamp() if departure is None else departure
        peak = 0.
        for a, b in zip(points, points[1:]):
            edge = self.edge(a, b)
            for visit in edge.visits:
                begin, end = stamp + visit.enter, stamp + visit.leave
                for at in (begin, *frame_boundaries(begin, end), end):
                    vector = self.vector(at, visit.row, visit.col)
                    if vector is None:
                        return math.inf
                    peak = max(peak, math.hypot(*vector))
            stamp += edge.duration
        return peak

    def diagnostics(self):
        return {**self.stats, "weather_loading_mode": self.loading_mode,
                "wind_time_model":self.dataset.time_model,"forecast_frame_count":len(self.dataset.frames),
                "spatial_risk_model": SPATIAL_RISK_MODEL,
                "grid_shape": [len(self.grid.lats), len(self.grid.lons)],
                "wind_field_bbox": [float(min(self.grid.lons)), float(min(self.grid.lats)),
                                    float(max(self.grid.lons)), float(max(self.grid.lats))],
                "forecast_hour_count": len(self.dataset.frames),
                "reach_radius_m": STEP * self.speed, "shear_halo_cells": 1,
                "source_io": self.dataset.io_stats.snapshot() if self.dataset.io_stats else {},
                "data_errors": sorted(self.failures)[:3]}
