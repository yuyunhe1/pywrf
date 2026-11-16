"""Timed fixed-route profiles using exactly the departure-window weather model."""
from __future__ import annotations

from bisect import bisect_right
import math

import numpy as np

from .departure_windows import _geometry, prepare_evaluation, describe_evaluation
from .wind_time import frame_boundaries, WIND_TIME_STEP_SECONDS, WIND_TIME_MODEL
from .gfs_provider import _current_utc
from .route_service import haversine_km, risk_name
from .wrf_timeline import ForecastDataset, get_dataset, iso, load_dataset, load_gfs_dataset, vector_at, cell_vector
from .grid_coverage import cell_visits, shear_contacts, SPATIAL_RISK_MODEL

COLORS = {"一级风": "#28c76f", "二级风": "#f4c95d", "三级风": "#ff9f43",
          "四级风": "#ea5455", "大于四级": "#9b51e0", "数据不足": "#94a3b8"}


class RouteGeometry:
    def __init__(self, points, speed):
        self.points, self.distance = _geometry(points)
        self.speed = speed
        self.offsets = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self.offsets.append(self.offsets[-1] + haversine_km(a, b) * 1000 / speed)
        self.duration = self.offsets[-1]
        if not math.isfinite(self.duration):
            raise ValueError("平均对地速度过小，无法计算有效飞行时长")

    def at(self, offset):
        index = max(0, min(len(self.points) - 2, bisect_right(self.offsets, offset) - 1))
        start, end = self.points[index:index + 2]
        span = self.offsets[index + 1] - self.offsets[index]
        # A requested anchor and its grid centre can differ by one floating
        # point bit yet have zero geodesic distance. Such a segment has no
        # flight duration and must not divide by zero during profile rendering.
        fraction = min(1.0, max(0.0, (offset - self.offsets[index]) / span)) if span > 0 else 1.0
        lon, lat = (start[axis] + fraction * (end[axis] - start[axis]) for axis in (0, 1))
        heading = math.atan2((end[0] - start[0]) * math.cos(math.radians(lat)), end[1] - start[1])
        return lon, lat, heading

    def cuts(self, dataset):
        cuts = set(self.offsets)
        if dataset.frames:
            grid = dataset.frames[0][1]
            edges = [(axis[1:] + axis[:-1]) / 2 for axis in (grid.lons, grid.lats)]
            edges = [np.concatenate(([min(axis)], middle, [max(axis)]))
                     for axis, middle in zip((grid.lons, grid.lats), edges)]
            for index, (a, b) in enumerate(zip(self.points, self.points[1:])):
                for dimension, boundaries in enumerate(edges):
                    delta = b[dimension] - a[dimension]
                    if delta:
                        for boundary in boundaries:
                            fraction = (boundary - a[dimension]) / delta
                            if 0 < fraction < 1:
                                cuts.add(self.offsets[index] + fraction * (self.offsets[index + 1] - self.offsets[index]))
        return cuts


def build_profile(route: dict, request, dataset: ForecastDataset) -> dict:
    if dataset.level != route["level"]:
        raise ValueError("风场评估高度与固定航线高度不一致，请重新评估")
    if (route.get("source") or request.source or "wrf") != dataset.source:
        raise ValueError("风场数据源与固定航线不一致，请重新评估")
    if dataset.source == "gfs" and "average_ground_speed_mps" not in request.model_fields_set:
        request = request.model_copy(update={"average_ground_speed_mps": 50.0})
    speed, thresholds = request.average_ground_speed_mps, request.thresholds
    geometry = RouteGeometry(route["points"], speed)
    departure = request.departure_at.timestamp()
    if departure + geometry.duration >= 253402300799:
        raise ValueError("飞行时长超出可表示时间范围，请增大平均对地速度")
    shear = request.wind_shear
    shear_limit = shear.horizontal.hard_delta_wind_vector_ms if shear.enabled and shear.horizontal.enabled and shear.horizontal.hard_constraint_enabled else None
    # Publication validates this voyage, not every possible future departure.
    # Fixed-route windows still evaluate the complete pinned horizon separately.
    selected = set()
    for index, (stamp, _) in enumerate(dataset.frames):
        if departure <= stamp <= departure + geometry.duration:
            selected.add(index)
    before = [i for i, (stamp, _) in enumerate(dataset.frames) if stamp <= departure]
    after = [i for i, (stamp, _) in enumerate(dataset.frames) if stamp >= departure + geometry.duration]
    if before:
        selected.add(before[-1])
    if after:
        selected.add(after[0])
    evaluation_frames = [dataset.frames[i] for i in sorted(selected)]
    evaluation = prepare_evaluation(geometry.points, evaluation_frames, speed, thresholds.danger, shear_limit,
                                    dataset.coverage_start, dataset.coverage_end, source=dataset.source,
                                    wind_time_model=dataset.time_model)
    if dataset.error:
        evaluation.data_error=dataset.error
    description = describe_evaluation(evaluation, request.departure_at, speed)
    decision = description["current_departure"]
    decision["reason"] = decision["reason"].replace("按当前时刻起飞", "按所选时间起飞")
    issues = [issue for issue in evaluation.issues if issue.departure.contains(departure)]
    cuts = geometry.cuts(dataset)
    cuts.update(stamp - departure for stamp in frame_boundaries(departure, departure + geometry.duration))
    cuts.update(stamp - departure for stamp, _ in dataset.frames if 0 < stamp - departure < geometry.duration)
    for issue in issues:
        cuts.update(value for value in (max(issue.visit.enter, issue.weather.start - departure),
                                        min(issue.visit.leave, issue.weather.end - departure))
                    if 0 < value < geometry.duration)
    # Grid crossings and every ten-minute switch fully partition held wind.

    def classify(offset, vector):
        if vector is None:
            return "unknown", "数据不足", f"该位置或经过时间缺少 {dataset.source.upper()} 风场数据"
        matching = [issue for issue in issues if issue.visit.enter <= offset <= issue.visit.leave
                    and issue.weather.contains(departure + offset)]
        if any(issue.kind == "missing" for issue in matching):
            return "unknown", "数据不足", next(issue.reason for issue in matching if issue.kind == "missing")
        if matching:
            return "blocked", risk_name(math.hypot(*vector), thresholds), matching[0].reason
        magnitude = math.hypot(*vector)
        if magnitude >= thresholds.danger:
            return "blocked", risk_name(magnitude, thresholds), f"风速达到或超过 {thresholds.danger:g} m/s 硬上限"
        return "allowed", risk_name(magnitude, thresholds), "未触发风速或水平风切变硬约束"

    segments = []
    for a, b in zip(sorted(cuts), sorted(cuts)[1:]):
        if b <= a:
            continue
        lon, lat, _ = geometry.at((a + b) / 2)
        status, risk, reason = classify((a + b) / 2, vector_at(dataset, departure + (a + b) / 2, lon, lat))
        segments.append({"start_seconds": a, "end_seconds": b,
                         "points": [list(geometry.at(a)[:2]), list(geometry.at(b)[:2])],
                         "status": status, "risk": risk, "reason": reason,
                         "color": COLORS["数据不足"] if status == "unknown" else COLORS[risk]})

    # Include cell boundaries, weather/risk boundaries, original waypoints and
    # their one-sided limits. A category chart can then depict spatial jumps.
    sample_cuts = set(cuts)
    step = max(15.0, geometry.duration / 1600)
    sample_cuts.update(float(value) for value in np.arange(0, geometry.duration, step))
    for value in cuts:
        sample_cuts.update(value + delta for delta in (-.001, .001) if 0 < value + delta < geometry.duration)
    samples = []
    for offset in sorted(sample_cuts):
        lon, lat, heading = geometry.at(offset)
        vector = vector_at(dataset, departure + offset, lon, lat)
        status, risk, reason = classify(offset, vector)
        u, v = vector if vector else (None, None)
        samples.append({"lon": lon, "lat": lat, "distance_km": offset * speed / 1000,
                        "elapsed_seconds": offset, "eta": iso(departure + offset), "u": u, "v": v,
                        "wind_speed": math.hypot(u, v) if vector else None,
                        "headwind_component": u * math.sin(heading) + v * math.cos(heading) if vector else None,
                        "crosswind_component": abs(u * math.cos(heading) - v * math.sin(heading)) if vector else None,
                        "flight_heading": math.degrees(heading) % 360, "risk": risk,
                        "status": status, "reason": reason})

    # Wind shear is a spatial difference at ONE time, never the difference
    # between two samples encountered at different times.
    shear_profile, events = [], []
    grid = dataset.frames[0][1] if dataset.frames else None
    contacts = ()
    if grid is not None:
        try:
            contacts = shear_contacts(cell_visits(geometry.points,grid,speed))
        except ValueError:
            pass  # The independent decision already reports outside coverage.
    by_span = {}
    for a,b,contact in contacts:
        probes = (contact.enter, *[stamp-departure for stamp in frame_boundaries(
            departure+contact.enter,departure+contact.leave)], contact.leave)
        values = []
        for offset in probes:
            left = cell_vector(dataset,departure+offset,*a)
            right = cell_vector(dataset,departure+offset,*b)
            if left is not None and right is not None:
                values.append((math.hypot(right[0]-left[0],right[1]-left[1]),offset))
        if values:
            value = max(values)
            key = contact.enter,contact.leave
            if key not in by_span or value[0] > by_span[key][0]:
                by_span[key] = value
    for (begin,end),(delta,offset) in sorted(by_span.items()):
        distance = offset*speed/1000
        hard = shear_limit is not None and delta >= shear_limit
        shear_profile.append({"start_distance_km": begin*speed/1000, "end_distance_km": end*speed/1000,
                              "center_distance_km": distance, "segment_distance_km": (end-begin)*speed/1000,
                              "horizontal_wind_shear": delta, "delta_wind_vector_ms": delta,
                              "eta": iso(departure+offset), "hard_constraint_triggered": hard})
        if hard:
            lon,lat,_ = geometry.at(offset)
            events.append({"lon":lon,"lat":lat,"elapsed_seconds":offset,"eta":iso(departure+offset),
                           "reason":f"水平风切变 {delta:.2f} m/s，达到硬上限","type":"shear"})
    for issue in issues:
        if issue.kind == "wind" and issue.visit.enter == issue.visit.leave:
            offset = issue.visit.enter
            lon,lat,_ = geometry.at(offset)
            if not any(event.get("type")=="wind" and event["elapsed_seconds"]==offset for event in events):
                events.append({"lon":lon,"lat":lat,"elapsed_seconds":offset,"eta":iso(departure+offset),
                               "reason":issue.reason,"type":"wind"})

    known = [s["wind_speed"] for s in samples if s["wind_speed"] is not None]
    known_duration, weighted_wind, tailwind_duration, high_duration = 0.0, 0.0, 0.0, 0.0
    continuous_high = max_high = 0.0
    for segment in segments:
        length = segment["end_seconds"] - segment["start_seconds"]
        middle = (segment["start_seconds"] + segment["end_seconds"]) / 2
        lon, lat, heading = geometry.at(middle)
        vector = vector_at(dataset, departure + middle, lon, lat)
        if vector is not None:
            magnitude = math.hypot(*vector)
            known_duration += length
            weighted_wind += magnitude * length
            tailwind_duration += length if vector[0] * math.sin(heading) + vector[1] * math.cos(heading) >= 0 else 0
            if magnitude > thresholds.warning:
                high_duration += length
                continuous_high += length * speed / 1000
                max_high = max(max_high, continuous_high)
            else:
                continuous_high = 0
        else:
            continuous_high = 0
    incomplete = decision["status"] == "unknown"
    shear_max = max((item["horizontal_wind_shear"] for item in shear_profile), default=None)
    analysis = {"samples": samples, "total_distance_km": geometry.distance / 1000,
                "max_wind_speed": max(known) if known else None,
                "mean_wind_speed": weighted_wind / known_duration if known_duration else None,
                "danger_ratio": high_duration / geometry.duration, "tailwind_ratio": tailwind_duration / geometry.duration,
                "unknown_ratio": max(0.0, 1 - known_duration / geometry.duration), "coverage_incomplete": incomplete,
                "max_continuous_danger_km": max_high,
                "risk_level": "数据不足" if incomplete else risk_name(max(known), thresholds) if known else "数据不足",
                "navigation_allowed": decision["status"] == "allowed", "navigation_status": decision["status"],
                "navigation_decision_reason": decision["reason"],
                "wind_shear": {"enabled": shear_limit is not None, "max_horizontal_wind_shear": shear_max,
                               "horizontal_shear_warning_count": sum(event["type"]=="shear" for event in events),
                               "horizontal_shear_evaluation_count": len(shear_profile),
                               "horizontal_wind_shear_profile": shear_profile}}
    bbox = [min(p[0] for p in geometry.points), min(p[1] for p in geometry.points),
            max(p[0] for p in geometry.points), max(p[1] for p in geometry.points)]
    return {"route_id": route["route_id"], "route_name": route["name"], "dataset_id": dataset.dataset_id,
            "wind_time_step_seconds": WIND_TIME_STEP_SECONDS, "wind_time_model": dataset.time_model,
            "spatial_risk_model": SPATIAL_RISK_MODEL,
            "forecast_cycle": dataset.cycle, "level": route["level"], "departure_at": iso(departure), "source": dataset.source,
            "arrival_at": iso(departure + geometry.duration), "average_ground_speed_mps": speed,
            "flight_duration_seconds": geometry.duration, "decision": decision, "analysis": analysis,
            "segments": segments, "events": events, "bbox": bbox,
            "frame_times": [iso(stamp) for stamp, _ in dataset.frames]}


def evaluate_flight(route, request):
    source = route.get("source") or request.source or "wrf"
    if request.dataset_id:
        dataset=get_dataset(request.dataset_id)
    else:
        from . import wrf_cache_provider as wrf, gfs_provider as gfs
        cycle=request.cycle
        if not cycle and request.valid_time:
            cycle=(gfs.find_file_by_valid_time(request.valid_time).cycle if source=='gfs'
                   else wrf._find_record_by_valid_time(request.valid_time)['cycle'])
        loader=load_gfs_dataset if source=='gfs' else load_dataset
        dataset=loader(route['level'],_current_utc(),cycle) if cycle else loader(route['level'],_current_utc())
    return build_profile(route, request, dataset)
