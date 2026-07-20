"""Route sampling and wind-risk statistics."""

from math import asin, cos, radians, sin, sqrt

import numpy as np

from .models import Thresholds
from .wind_provider import WindGrid, point_value

EARTH_RADIUS_KM = 6371.0
POINT_EPSILON = 1e-10


def haversine_km(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Calculate great-circle distance for [lon, lat] points."""
    lon1, lat1, lon2, lat2 = map(radians, (start[0], start[1], end[0], end[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(value))


def anchor_route_endpoints(
    grid_points: list[tuple[float, float]],
    start: tuple[float, float],
    end: tuple[float, float],
) -> list[tuple[float, float]]:
    """Replace the first/last grid centres with the requested map points.

    Grid planners search cell centres internally.  Adding the requested points
    before and after those centres creates artificial first/last turns.  The
    centres represent the same start/goal cells, so replacing them preserves
    the searched cell corridor while returning the actual flight geometry.
    """

    start_point = (float(start[0]), float(start[1]))
    end_point = (float(end[0]), float(end[1]))
    centres = [(float(point[0]), float(point[1])) for point in grid_points]

    # Remove repeated centres, including the duplicate produced when start and
    # goal map to the same grid cell.
    unique_centres: list[tuple[float, float]] = []
    for point in centres:
        if not unique_centres or not _same_point(unique_centres[-1], point):
            unique_centres.append(point)

    if len(unique_centres) <= 1:
        anchored = [start_point, end_point]
    else:
        anchored = [start_point, *unique_centres[1:-1], end_point]

    points: list[tuple[float, float]] = []
    for point in anchored:
        if not points or not _same_point(points[-1], point):
            points.append(point)
    return points


def _same_point(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return abs(left[0] - right[0]) <= POINT_EPSILON and abs(left[1] - right[1]) <= POINT_EPSILON


def risk_name(speed: float, thresholds: Thresholds) -> str:
    """Map a wind speed in m/s to a Chinese risk label."""
    if speed <= thresholds.safe:
        return "一级风"
    if speed <= thresholds.notice:
        return "二级风"
    if speed <= thresholds.warning:
        return "三级风"
    if speed <= thresholds.danger:
        return "四级风"
    return "大于四级"


def sample_route(
    points: list[tuple[float, float]], grid: WindGrid, thresholds: Thresholds, interval_km: float
) -> list[dict]:
    """Sample every route segment at an approximately constant distance."""
    from math import atan2, degrees
    
    samples: list[dict] = []
    cumulative = 0.0
    for segment_index, (start, end) in enumerate(zip(points, points[1:])):
        distance = haversine_km(start, end)
        if distance == 0:
            continue
            
        # 计算这段航线的真实飞行航向角 (正北为 0，顺时针 0-360)
        d_lon = end[0] - start[0]
        d_lat = end[1] - start[1]
        flight_heading = (degrees(atan2(d_lon, d_lat)) + 360) % 360
        
        steps = max(1, int(np.ceil(distance / interval_km)))
        for step in range(steps + 1):
            if segment_index > 0 and step == 0:
                continue
            fraction = step / steps
            lon = start[0] + (end[0] - start[0]) * fraction
            lat = start[1] + (end[1] - start[1]) * fraction
            wind = point_value(grid, lon, lat)
            # Replace lon/lat in wind dict with the exact geometric coordinates rather than nearest grid center
            wind["lon"] = lon
            wind["lat"] = lat
            wind["distance_km"] = round(cumulative + distance * fraction, 3)
            wind["risk"] = risk_name(wind["wind_speed"], thresholds)
            
            # 计算顺风/逆风效应
            # wind_direction_to 也是以正北为 0 的去向角，两者夹角差小于 90 度为顺风，大于 90 度为逆风
            angle_diff = abs((wind["wind_direction_to"] - flight_heading + 180) % 360 - 180)
            wind["is_tailwind"] = angle_diff <= 90
            wind["headwind_component"] = round(wind["wind_speed"] * cos(radians(angle_diff)), 2)
            
            # 计算侧风分量 (横风)
            wind["crosswind_component"] = round(wind["wind_speed"] * sin(radians(angle_diff)), 2)
            wind["flight_heading"] = round(flight_heading, 1)
            
            samples.append(wind)
        cumulative += distance
    return samples


def analyze_route(samples: list[dict], thresholds: Thresholds) -> dict:
    """Summarize sampled route risk."""
    if not samples:
        return {}
    
    speeds = np.array([sample["wind_speed"] for sample in samples])
    danger_ratio = float(np.mean(speeds > thresholds.warning))
    total_distance = samples[-1]["distance_km"] if samples else 0.0
    
    # Calculate max continuous distance above warning threshold
    max_continuous_danger_km = 0.0
    current_continuous = 0.0
    
    # Analyze tailwind/headwind
    tailwind_count = sum(1 for s in samples if s.get("is_tailwind", False))
    tailwind_ratio = tailwind_count / len(samples) if samples else 0.0
    
    for i in range(1, len(samples)):
        if samples[i]["wind_speed"] > thresholds.warning:
            current_continuous += (samples[i]["distance_km"] - samples[i-1]["distance_km"])
            max_continuous_danger_km = max(max_continuous_danger_km, current_continuous)
        else:
            current_continuous = 0.0
            
    return {
        "max_wind_speed": round(float(speeds.max()), 2),
        "mean_wind_speed": round(float(speeds.mean()), 2),
        "danger_ratio": round(danger_ratio, 3),
        "risk_level": risk_name(float(speeds.max()), thresholds),
        "total_distance_km": round(total_distance, 1),
        "max_continuous_danger_km": round(max_continuous_danger_km, 1),
        "tailwind_ratio": round(tailwind_ratio, 3),
        "samples": samples,
    }


def include_wind_shear_in_high_risk_ratio(
    analysis: dict,
    samples: list[dict],
    wind_shear: dict | None,
    thresholds: Thresholds,
) -> dict:
    """Combine node wind risk and horizontal-edge shear risk.

    Wind samples represent node/grid-cell properties.  Horizontal wind-shear
    evaluations represent consecutive route edges.  Vertical shear is not
    included in the current planning or risk model.
    """

    if not analysis:
        analysis = {}
    wind_sample_count = len(samples)
    wind_high_risk_count = sum(float(sample.get("wind_speed", 0.0)) > thresholds.warning for sample in samples)
    wind_only_ratio = wind_high_risk_count / wind_sample_count if wind_sample_count else 0.0

    shear_enabled = bool(wind_shear and wind_shear.get("enabled", False))
    shear_evaluation_count = 0
    shear_high_risk_count = 0
    if shear_enabled:
        shear_evaluation_count = int(wind_shear.get("horizontal_shear_evaluation_count", 0) or 0)
        shear_high_risk_count = int(wind_shear.get("horizontal_shear_warning_count", 0) or 0)

    total_evaluations = wind_sample_count + shear_evaluation_count
    combined_high_risk_count = wind_high_risk_count + shear_high_risk_count
    combined_ratio = combined_high_risk_count / total_evaluations if total_evaluations else 0.0
    analysis.update(
        {
            "wind_only_danger_ratio": round(float(wind_only_ratio), 3),
            "wind_shear_risk_ratio": round(
                float(shear_high_risk_count / shear_evaluation_count) if shear_evaluation_count else 0.0,
                3,
            ),
            "danger_ratio": round(float(combined_ratio), 3),
            "high_risk_evaluation_count": combined_high_risk_count,
            "risk_evaluation_count": total_evaluations,
        }
    )
    return analysis
