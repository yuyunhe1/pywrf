"""Route sampling and wind-risk statistics."""

from math import asin, cos, radians, sin, sqrt

import numpy as np

from .models import Thresholds
from .wind_provider import WindGrid, point_value

EARTH_RADIUS_KM = 6371.0


def haversine_km(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Calculate great-circle distance for [lon, lat] points."""
    lon1, lat1, lon2, lat2 = map(radians, (*start, *end))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(value))


def risk_name(speed: float, thresholds: Thresholds) -> str:
    """Map a wind speed in m/s to a Chinese risk label."""
    if speed <= thresholds.safe:
        return "安全"
    if speed <= thresholds.notice:
        return "注意"
    if speed <= thresholds.warning:
        return "中等风险"
    if speed <= thresholds.danger:
        return "高风险"
    return "危险"


def sample_route(
    points: list[tuple[float, float]], grid: WindGrid, thresholds: Thresholds, interval_km: float
) -> list[dict]:
    """Sample every route segment at an approximately constant distance."""
    samples: list[dict] = []
    cumulative = 0.0
    for segment_index, (start, end) in enumerate(zip(points, points[1:])):
        distance = haversine_km(start, end)
        steps = max(1, int(np.ceil(distance / interval_km)))
        for step in range(steps + 1):
            if segment_index > 0 and step == 0:
                continue
            fraction = step / steps
            lon = start[0] + (end[0] - start[0]) * fraction
            lat = start[1] + (end[1] - start[1]) * fraction
            wind = point_value(grid, lon, lat)
            wind["distance_km"] = round(cumulative + distance * fraction, 3)
            wind["risk"] = risk_name(wind["wind_speed"], thresholds)
            samples.append(wind)
        cumulative += distance
    return samples


def analyze_route(samples: list[dict], thresholds: Thresholds) -> dict:
    """Summarize sampled route risk."""
    speeds = np.array([sample["wind_speed"] for sample in samples])
    danger_ratio = float(np.mean(speeds > thresholds.warning))
    return {
        "max_wind_speed": round(float(speeds.max()), 2),
        "mean_wind_speed": round(float(speeds.mean()), 2),
        "danger_ratio": round(danger_ratio, 3),
        "risk_level": risk_name(float(speeds.max()), thresholds),
        "samples": samples,
    }
