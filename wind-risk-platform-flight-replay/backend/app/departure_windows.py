"""Continuous departure windows on a fixed route and ten-minute held vectors.

Read native WRF frames (or sample legacy/GFS U/V), then shift unsafe weather times by
each touched closed cell's flight occupancy interval. This excludes hazards
between the displayed five-minute departure boundaries, without route search.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from threading import RLock

import numpy as np

from .gfs_provider import _current_utc
from .route_service import haversine_km
from .wind_provider import WindGrid
from .wrf_timeline import load_dataset, load_gfs_dataset
from .wind_time import held_vectors, WIND_TIME_STEP_SECONDS, WIND_TIME_MODEL
from .grid_coverage import CellVisit, cell_visits as _cell_visits, shear_contacts, SPATIAL_RISK_MODEL
from .planning_control import checkpoint

DISPLAY_STEP_SECONDS = 300
_CACHE: OrderedDict = OrderedDict()
_CACHE_LOCK = RLock()


def _iso(epoch: float | None) -> str | None:
    if epoch is None or not math.isfinite(epoch):
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch(label: str) -> float:
    return datetime.strptime(label, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).timestamp()


@dataclass(frozen=True)
class Interval:
    start: float
    end: float
    left_closed: bool = True
    right_closed: bool = True

    def contains(self, value: float) -> bool:
        return (value > self.start or self.left_closed and value == self.start) and (
            value < self.end or self.right_closed and value == self.end)

    def valid(self) -> bool:
        return self.start < self.end or self.start == self.end and self.left_closed and self.right_closed


def _intersection(a: Interval, b: Interval) -> Interval | None:
    start, end = max(a.start, b.start), min(a.end, b.end)
    left = (a.left_closed if start == a.start else True) and (b.left_closed if start == b.start else True)
    right = (a.right_closed if end == a.end else True) and (b.right_closed if end == b.end else True)
    result = Interval(start, end, left, right)
    return result if result.valid() else None


def _merge(intervals: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for current in sorted((item for item in intervals if item.valid()), key=lambda item: (item.start, not item.left_closed)):
        if not result:
            result.append(current)
            continue
        previous = result[-1]
        if current.start < previous.end or current.start == previous.end and (current.left_closed or previous.right_closed):
            end = max(current.end, previous.end)
            right = (current.right_closed if current.end == end else False) or (previous.right_closed if previous.end == end else False)
            result[-1] = Interval(previous.start, end, previous.left_closed, right)
        else:
            result.append(current)
    return result


def _complement(domain: Interval, excluded: list[Interval]) -> list[Interval]:
    if not domain.valid():
        return []
    clipped = [part for item in excluded if (part := _intersection(domain, item)) is not None]
    cursor, closed = domain.start, domain.left_closed
    result = []
    for item in _merge(clipped):
        part = Interval(cursor, item.start, closed, not item.left_closed)
        if part.valid():
            result.append(part)
        cursor, closed = item.end, not item.right_closed
    part = Interval(cursor, domain.end, closed, domain.right_closed)
    if part.valid():
        result.append(part)
    return result


def _vector_issues(times, values, limit, time_model=WIND_TIME_MODEL):
    """Exact unsafe/missing intervals for piecewise constant ten-minute frames."""
    missing, unsafe = [], []
    for index, (start, end) in enumerate(zip(times, times[1:])):
        checkpoint()
        for begin, finish, vector in held_vectors(start, end, values[index], values[index + 1],time_model):
            if vector is None:
                missing.append(Interval(begin, finish, True, False))
            elif math.hypot(*vector) >= limit:
                unsafe.append(Interval(begin, finish, True, False))
    for stamp, vector in zip(times, values):
        if not all(map(math.isfinite, vector)):
            missing.append(Interval(stamp, stamp))
        elif math.hypot(*vector) >= limit:
            unsafe.append(Interval(stamp, stamp))
    return _merge(missing), _merge(unsafe)


@dataclass(frozen=True)
class Issue:
    departure: Interval
    weather: Interval
    kind: str
    reason: str
    visit: CellVisit


@dataclass
class Evaluation:
    distance_m: float
    duration_s: float
    coverage_start: float | None
    coverage_end: float | None
    issues: list[Issue]
    data_error: str | None = None
    wind_time_model: str = WIND_TIME_MODEL


def _geometry(points) -> tuple[list[tuple[float, float]], float]:
    cleaned = []
    for point in points:
        if len(point) < 2:
            raise ValueError("固定航线缺少有效经纬度")
        lon, lat = float(point[0]), float(point[1])
        if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError("固定航线包含无效经纬度")
        if not cleaned or (lon, lat) != cleaned[-1]:
            cleaned.append((lon, lat))
    distance = sum(haversine_km(a, b) * 1000 for a, b in zip(cleaned, cleaned[1:]))
    if distance <= 0:
        raise ValueError("固定航线至少需要两个不同位置，且总里程必须大于零")
    return cleaned, distance


def prepare_evaluation(points, frames: list[tuple[float, WindGrid]], speed: float, wind_limit: float,
                       shear_limit: float | None, coverage_start=None, coverage_end=None, source="wrf",
                       wind_time_model=WIND_TIME_MODEL) -> Evaluation:
    label = source.upper()
    points, distance = _geometry(points)
    if not math.isfinite(distance / speed):
        raise ValueError("平均对地速度过小，无法计算有效飞行时长")
    evaluation = Evaluation(distance, distance / speed, coverage_start, coverage_end, [],wind_time_model=wind_time_model)
    frames = sorted((stamp, grid) for stamp, grid in frames
                    if grid.u.shape == grid.v.shape == (len(grid.lats), len(grid.lons)))
    if not frames:
        evaluation.data_error = f"缺少可用的 {label} 风场，无法评估完整飞行"
        return evaluation
    reference = frames[0][1]
    evaluation.wind_time_model=reference.wind_time_model
    if any(grid.wind_time_model!=reference.wind_time_model for _,grid in frames):
        evaluation.data_error='同一评估中不能混合 WRF 时间模型'
        return evaluation
    frames = [(stamp, grid) for stamp, grid in frames if np.array_equal(grid.lons, reference.lons)
              and np.array_equal(grid.lats, reference.lats)]
    evaluation.coverage_start = coverage_start if coverage_start is not None else frames[0][0]
    evaluation.coverage_end = coverage_end if coverage_end is not None else frames[-1][0]
    try:
        visits = _cell_visits(points, reference, speed)
    except ValueError as exc:
        evaluation.data_error = str(exc)
        return evaluation

    def add(weather: Interval, visit: CellVisit, kind: str, reason: str):
        departure = Interval(weather.start - visit.leave, weather.end - visit.enter, weather.left_closed, weather.right_closed)
        evaluation.issues.append(Issue(departure, weather, kind, reason, visit))

    times = [stamp for stamp, _ in frames]
    for visit in visits:
        checkpoint()
        add(Interval(-math.inf, times[0], True, False), visit, "missing", f"{label} 预报未覆盖完整飞行")
        add(Interval(times[-1], math.inf, False, True), visit, "missing", f"飞行结束超出 {label} 预报覆盖范围")
        values = [(float(grid.u[visit.row, visit.col]), float(grid.v[visit.row, visit.col])) for _, grid in frames]
        missing, unsafe = _vector_issues(times, values, wind_limit,evaluation.wind_time_model)
        for weather in missing:
            add(weather, visit, "missing", f"沿途 {label} 时次缺失或风矢量缺测")
        for weather in unsafe:
            add(weather, visit, "wind", f"沿途风速达到或超过 {wind_limit:g} m/s 硬上限")

    if shear_limit is not None:
        for left, right, crossing in shear_contacts(visits):
            differences = [(float(grid.u[right] - grid.u[left]), float(grid.v[right] - grid.v[left]))
                           for _, grid in frames]
            missing, unsafe = _vector_issues(times, differences, shear_limit,evaluation.wind_time_model)
            for weather in missing:
                add(weather, crossing, "missing", f"沿途 {label} 时次缺失或风矢量缺测")
            for weather in unsafe:
                add(weather, crossing, "shear", f"沿途水平风切变达到或超过 {shear_limit:g} m/s 硬上限")
    return evaluation


def describe_evaluation(evaluation: Evaluation, now: datetime, speed: float, departure_end: datetime | None = None) -> dict:
    stamp = now.timestamp()
    end = evaluation.coverage_end
    last_departure = end - evaluation.duration_s if end is not None else None
    domain = Interval(stamp, min(last_departure, departure_end.timestamp()) if last_departure is not None and departure_end is not None
                      else last_departure if last_departure is not None else stamp - 1)
    unknown = [item for item in evaluation.issues if item.kind == "missing"]
    matching = [item for item in evaluation.issues if item.departure.contains(stamp)]
    missing_now = [item for item in matching if item.kind == "missing"]
    current_status = "unknown" if evaluation.data_error or missing_now else "blocked" if matching else "allowed"
    first_issue = None
    reason = evaluation.data_error
    if matching:
        issue = min(missing_now or matching, key=lambda item: max(stamp + item.visit.enter, item.weather.start))
        reason = issue.reason
        encounter = max(stamp + issue.visit.enter, issue.weather.start)
        fraction = 0 if issue.visit.leave == issue.visit.enter else (encounter - stamp - issue.visit.enter) / (issue.visit.leave - issue.visit.enter)
        first_issue = {
            "type": issue.kind, "at": _iso(encounter), "distance_km": (encounter - stamp) * speed / 1000,
            "lon": issue.visit.start_point[0] + fraction * (issue.visit.end_point[0] - issue.visit.start_point[0]),
            "lat": issue.visit.start_point[1] + fraction * (issue.visit.end_point[1] - issue.visit.start_point[1]),
        }
    if reason is None:
        reason = "按当前时刻起飞，全程未触发风速或水平风切变硬约束"
    safe = [] if evaluation.data_error else _complement(domain, [item.departure for item in evaluation.issues])
    windows = []
    for interval in safe:
        start = math.ceil(interval.start / DISPLAY_STEP_SECONDS) * DISPLAY_STEP_SECONDS
        finish = math.floor(interval.end / DISPLAY_STEP_SECONDS) * DISPLAY_STEP_SECONDS
        if not interval.contains(start):
            start += DISPLAY_STEP_SECONDS
        if not interval.contains(finish):
            finish -= DISPLAY_STEP_SECONDS
        if start <= finish:
            windows.append({"start": _iso(start), "end": _iso(finish),
                            "first_arrival": _iso(start + evaluation.duration_s), "last_arrival": _iso(finish + evaluation.duration_s)})
    partial = evaluation.data_error is not None or any(_intersection(domain, item.departure) is not None for item in unknown)
    events = [edge for item in evaluation.issues for edge in (item.departure.start, item.departure.end)
              if math.isfinite(edge) and edge >= stamp]
    refresh_at = min([stamp + 60, *events])
    if refresh_at <= stamp:
        refresh_at = stamp + 0.001
    empty_reason = None if windows else "data_insufficient" if partial or last_departure is None or last_departure < stamp else "short_window" if safe else "conditions_unsuitable"
    return {
        "evaluated_at": _iso(stamp), "refresh_at": _iso(refresh_at), "source": "wrf",
        "average_ground_speed_mps": speed, "route_distance_km": evaluation.distance_m / 1000,
        "flight_duration_seconds": evaluation.duration_s, "display_step_minutes": 5,
        "wind_time_step_seconds": WIND_TIME_STEP_SECONDS, "wind_time_model": evaluation.wind_time_model,
        "spatial_risk_model": SPATIAL_RISK_MODEL,
        "coverage_start": _iso(evaluation.coverage_start), "coverage_end": _iso(end),
        "latest_departure": _iso(last_departure), "coverage_incomplete": partial,
        "current_departure": {"status": current_status, "departure_at": _iso(stamp),
                              "arrival_at": _iso(stamp + evaluation.duration_s), "reason": reason, "first_issue": first_issue},
        "departure_windows": windows, "empty_reason": empty_reason,
    }


def evaluate_saved_route(route: dict, request, now: datetime | None = None, dataset=None) -> dict:
    now = now or _current_utc()
    source = route.get("source") or request.source or "wrf"
    if source == "gfs" and "average_ground_speed_mps" not in request.model_fields_set:
        request = request.model_copy(update={"average_ground_speed_mps": 50.0})
    if dataset is None:
        from . import wrf_cache_provider as wrf, gfs_provider as gfs
        cycle = request.cycle
        if not cycle and request.valid_time:
            cycle = (gfs.find_file_by_valid_time(request.valid_time).cycle if source == 'gfs'
                     else wrf._find_record_by_valid_time(request.valid_time)['cycle'])
        dataset = (load_gfs_dataset(route['level'],now,cycle) if source=='gfs' else
                   load_dataset(route['level'],now,cycle) if cycle else load_dataset(route['level'],now))
    if dataset.source != source:
        raise ValueError("风场数据源与固定航线不一致，请重新评估")
    if dataset.level != route["level"]:
        raise ValueError("风场评估高度与航线高度不一致，请重新评估")
    speed = request.average_ground_speed_mps
    shear = request.wind_shear
    shear_limit = shear.horizontal.hard_delta_wind_vector_ms if shear.enabled and shear.horizontal.enabled and shear.horizontal.hard_constraint_enabled else None
    key = (SPATIAL_RISK_MODEL, dataset.dataset_id, tuple(tuple(point[:2]) for point in route["points"]), route["level"], speed,
           request.thresholds.danger, shear_limit)
    with _CACHE_LOCK:
        evaluation = _CACHE.get(key)
        if evaluation is None:
            evaluation = prepare_evaluation(route["points"], list(dataset.frames), speed, request.thresholds.danger, shear_limit,
                                            dataset.coverage_start, dataset.coverage_end, source=dataset.source,
                                            wind_time_model=dataset.time_model)
            if dataset.error:
                evaluation.data_error = dataset.error
            _CACHE[key] = evaluation
            while len(_CACHE) > 8:
                _CACHE.popitem(last=False)
        _CACHE.move_to_end(key)
    return {**describe_evaluation(evaluation, now, speed), "route_id": route["route_id"], "route_name": route["name"],
            "level": route["level"], "forecast_cycle": dataset.cycle, "dataset_id": dataset.dataset_id, "source": dataset.source,
            "wind_speed_limit_mps": request.thresholds.danger,
            "horizontal_wind_shear_limit_mps": shear_limit}
