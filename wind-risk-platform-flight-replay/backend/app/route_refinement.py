"""Retimed cycle removal and cost-preserving geometric shortcuts.

No grid-visit pruning: these operations concern a complete spatial curve.
Every proposal is checked from the original departure, including its suffix.
"""
from __future__ import annotations

import math
from time import monotonic
import numpy as np

from .planning_control import checkpoint
from .route_service import haversine_km

EPS = 1e-9


def clean_points(points):
    result = []
    for point in points:
        point = tuple(map(float, point[:2]))
        if not result or haversine_km(result[-1], point) > 1e-10:
            result.append(point)
    return result


def shape_quality(points):
    headings = [math.atan2((b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2)), b[1] - a[1])
                for a, b in zip(points, points[1:]) if haversine_km(a, b) > 1e-10]
    turns = [abs((b - a + math.pi) % (2 * math.pi) - math.pi) * 180 / math.pi
             for a, b in zip(headings, headings[1:])]
    return dict(max_turn_deg=max(turns, default=0.), turn_count=sum(x > 5 for x in turns),
                total_turn_deg=math.fsum(turns), u_turn_count=sum(x > 150 for x in turns), waypoint_count=len(points))


def shape_key(quality):
    return tuple(quality[key] for key in ("u_turn_count", "turn_count", "max_turn_deg", "total_turn_deg", "waypoint_count"))


def shape_not_worse(a, b):
    return all(a[key] <= b[key] + 1e-6 for key in ("max_turn_deg", "turn_count", "total_turn_deg", "u_turn_count"))


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def intersections(a, b, c, d):
    """Parameters on both segments, including positive collinear retracing."""
    v, w, offset = (b[0] - a[0], b[1] - a[1]), (d[0] - c[0], d[1] - c[1]), (c[0] - a[0], c[1] - a[1])
    det = _cross(v, w)
    if abs(det) > 1e-14:
        t, u = _cross(offset, w) / det, _cross(offset, v) / det
        if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
            yield max(0., min(1., t)), max(0., min(1., u))
        return
    if abs(_cross(offset, v)) > 1e-13:
        return
    axis = 0 if abs(v[0]) >= abs(v[1]) else 1
    if abs(v[axis]) < 1e-14 or abs(w[axis]) < 1e-14:
        return
    for point in (a, b, c, d):
        t, u = (point[axis] - a[axis]) / v[axis], (point[axis] - c[axis]) / w[axis]
        if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
            yield max(0., min(1., t)), max(0., min(1., u))


def cycle_proposals(points):
    offsets = [0.]
    for a, b in zip(points, points[1:]):
        offsets.append(offsets[-1] + haversine_km(a, b))
    seen, proposals = set(), []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        checkpoint()
        for j in range(i + 1, len(points) - 1):
            c, d = points[j:j + 2]
            for t, u in intersections(a, b, c, d):
                removed = offsets[j] + u * (offsets[j + 1] - offsets[j]) - offsets[i] - t * (offsets[i + 1] - offsets[i])
                if removed < .001:
                    continue
                point = tuple(x + t * (y - x) for x, y in zip(a, b))
                trial = clean_points([*points[:i + 1], point, *points[j + 1:]])
                key = tuple(trial)
                if len(trial) >= 2 and key not in seen:
                    seen.add(key)
                    proposals.append((removed, trial))
    return [p for _, p in sorted(proposals, key=lambda item: -item[0])]


class RouteRefiner:
    def __init__(self, weather, points, prefer_simple=False):
        self.weather = weather
        self.points = clean_points(points)
        self.cost = weather.route_cost(self.points)
        self.stats = dict(cycles_removed=0, cycle_checks=0, shortcut_checks=0, slices=0, max_slice_seconds=0.)
        self.before = shape_quality(self.points)
        self.done = False
        self.prefer_simple = prefer_simple
        self.iterator = self._steps()

    def _cycle_steps(self):
        # Delete the longest safe cycle, then recompute every passage time.
        # A failed deletion proves nothing about other arrival-time branches.
        while True:
            changed = False
            for trial in cycle_proposals(self.points):
                checkpoint()
                self.stats["cycle_checks"] += 1
                cost = self.weather.route_cost(trial)
                if math.isfinite(cost):
                    self.points, self.cost, changed = trial, cost, True
                    self.stats["cycles_removed"] += 1
                yield
                if changed:
                    break
            if not changed:
                break
    def _steps(self):
        if not math.isfinite(self.cost):
            return
        yield from self._cycle_steps()
        if self.prefer_simple:
            from .route_quality import assess_route_quality, quality_shortcuts
            while not assess_route_quality(self.points,self.weather.speed)['acceptable']:
                changed=False
                for trial in quality_shortcuts(self.points,self.weather.speed):
                    checkpoint()
                    self.stats['shortcut_checks']+=1
                    cost=self.weather.route_cost(trial)
                    # Like cycle deletion, safe reversal removal takes
                    # precedence over soft wind cost. Never skip retiming.
                    if math.isfinite(cost) and shape_not_worse(shape_quality(trial),shape_quality(self.points)):
                        self.points,self.cost,changed=trial,cost,True
                    yield
                    if changed: break
                if not changed: break
        i = 0
        while i < len(self.points) - 2:
            quality = shape_quality(self.points)
            for j in range(len(self.points) - 1, i + 1, -1):
                checkpoint()
                trial = self.points[:i + 1] + self.points[j:]
                if not shape_not_worse(shape_quality(trial), quality):
                    yield
                    continue
                self.stats["shortcut_checks"] += 1
                cost = self.weather.route_cost(trial)
                if cost <= self.cost + EPS:
                    self.points, self.cost = trial, cost
                    yield
                    break
                yield
            i += 1
        # Check adjacent native grid alternatives too, without forcing a waypoint.
        grid = self.weather.grid
        for i in range(1, len(self.points) - 1):
            quality = shape_quality(self.points)
            point = self.points[i]
            row, col = int(np.abs(grid.lats - point[1]).argmin()), int(np.abs(grid.lons - point[0]).argmin())
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if not 0 <= row + dr < len(grid.lats) or not 0 <= col + dc < len(grid.lons):
                        continue
                    alternate = (float(grid.lons[col + dc]), float(grid.lats[row + dr]))
                    trial = clean_points([*self.points[:i], alternate, *self.points[i + 1:]])
                    q = shape_quality(trial)
                    if len(trial) != len(self.points) or not shape_not_worse(q, quality):
                        yield
                        continue
                    cost = self.weather.route_cost(trial)
                    self.stats["shortcut_checks"] += 1
                    if cost < self.cost - EPS or cost <= self.cost + EPS and shape_key(q) < shape_key(quality):
                        self.points, self.cost, quality = trial, cost, q
                    yield
        yield from self._cycle_steps()

    def run_slice(self, seconds=.1):
        began = monotonic()
        while not self.done and monotonic() - began < seconds:
            checkpoint()
            try:
                next(self.iterator)
            except StopIteration:
                self.done = True
        self.stats["slices"] += 1
        self.stats["max_slice_seconds"] = max(self.stats["max_slice_seconds"], monotonic() - began)
        return self.done

    def finish(self):
        try:
            while not self.run_slice():
                checkpoint()
            return self.points, self.cost, {**self.stats, "before": self.before, "after": shape_quality(self.points)}
        finally:
            # A cancelled slice otherwise leaves self -> generator -> self,
            # retaining weather and its large edge/array caches until GC.
            self.iterator.close()
