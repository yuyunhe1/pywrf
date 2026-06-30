"""Shared wind-grid model and mock provider used by tests/development."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import numpy as np

CYCLES = ("2026-06-15 00:00 UTC", "2026-06-15 06:00 UTC")
FORECAST_HOURS = (1, 2, 3, 6, 9, 12)
LEVELS = (
    "10m AGL",
    "30m AGL",
    "50m AGL",
    "80m AGL",
    "100m AGL",
    "200m AGL",
    "300m AGL",
    "500m AGL",
    "800m AGL",
    "1000m AGL",
    "1500m AGL",
    "2000m AGL",
    "3000m AGL",
)
AVERAGE_LAYER = "250-350m AGL average"
DEFAULT_BBOX = (116.0, 29.0, 121.0, 34.0)
GRID_STEP = 0.1


@dataclass(frozen=True)
class WindGrid:
    """Regular lon/lat grid. Values are stored north-to-south, west-to-east."""

    lons: np.ndarray
    lats: np.ndarray
    u: np.ndarray
    v: np.ndarray
    cycle: str
    forecast_hour: int
    level: str
    valid_time: str
    source: str = "GFS mock"
    cycle_bj: str | None = None
    valid_time_bj: str | None = None


def normalize_level(level: str) -> str:
    """Normalize a short level such as 100m to the public AGL label."""
    if level.strip().lower() == AVERAGE_LAYER.lower():
        return AVERAGE_LAYER
    normalized = level.strip().upper().replace(" AGL", "")
    candidate = f"{normalized.lower()} agl"
    for available in LEVELS:
        if available.lower() == candidate:
            return available
    raise ValueError(f"不支持的高度层: {level}")


def validate_selection(cycle: str, forecast_hour: int, level: str) -> str:
    """Validate a requested mock dataset selection."""
    if cycle not in CYCLES:
        raise ValueError(f"不支持的起报时间 (cycle): {cycle}")
    if forecast_hour not in FORECAST_HOURS:
        raise ValueError(f"不支持的预报时效 (forecast hour): {forecast_hour}")
    return normalize_level(level)


def parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    """Parse an optional minLon,minLat,maxLon,maxLat selection."""
    if not bbox:
        return None
    try:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox.split(","))
    except ValueError as exc:
        raise ValueError("bbox 格式必须为 minLon,minLat,maxLon,maxLat") from exc
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("bbox 的最小值必须小于最大值")
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180 and -90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise ValueError("bbox 的经度范围必须在 -180 到 180 之间，纬度必须在 -90 到 90 之间")
    return min_lon, min_lat, max_lon, max_lat


def _valid_time(cycle: str, forecast_hour: int) -> str:
    cycle_time = datetime.strptime(cycle, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    return (cycle_time + timedelta(hours=forecast_hour)).strftime("%Y-%m-%d %H:%M UTC")


@lru_cache(maxsize=128)
def get_grid(cycle: str, forecast_hour: int, level: str, bbox: tuple[float, float, float, float] | None) -> WindGrid:
    """Generate and cache a deterministic mock wind field."""
    level = validate_selection(cycle, forecast_hour, level)
    if level == AVERAGE_LAYER:
        grids = [get_grid(cycle, forecast_hour, f"{height}m AGL", bbox) for height in (200, 300, 500)]
        # Interpolate 250/350 m from the adjacent native layers, then average 250-350 m.
        u250, v250 = (grids[0].u + grids[1].u) / 2, (grids[0].v + grids[1].v) / 2
        u350, v350 = (3 * grids[1].u + grids[2].u) / 4, (3 * grids[1].v + grids[2].v) / 4
        return WindGrid(grids[1].lons, grids[1].lats, (u250 + 4 * grids[1].u + u350) / 6, (v250 + 4 * grids[1].v + v350) / 6, cycle, forecast_hour, level, grids[1].valid_time, grids[1].source, grids[1].cycle_bj, grids[1].valid_time_bj)
    min_lon, min_lat, max_lon, max_lat = bbox or DEFAULT_BBOX
    lons = np.arange(min_lon, max_lon + GRID_STEP / 2, GRID_STEP)
    lats = np.arange(max_lat, min_lat - GRID_STEP / 2, -GRID_STEP)
    lon_grid, lat_grid = np.meshgrid(lons, lats)

    height = float(level.split("m")[0])
    cycle_hour = int(cycle[11:13])
    phase = (forecast_hour + cycle_hour) * np.pi / 12
    height_factor = 1 + height / 180
    u = height_factor * (3.8 + 2.2 * np.sin((lat_grid - 31.2) * 1.4 + phase))
    v = height_factor * (1.2 + 2.6 * np.cos((lon_grid - 118.4) * 1.25 - phase))
    u += 2.5 * np.exp(-((lon_grid - 119.2) ** 2 + (lat_grid - 31.5) ** 2) / 0.7)
    v -= 1.8 * np.exp(-((lon_grid - 117.5) ** 2 + (lat_grid - 32.2) ** 2) / 0.9)

    valid_time = _valid_time(cycle, forecast_hour)
    cycle_time = datetime.strptime(cycle, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    valid_dt = datetime.strptime(valid_time, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    return WindGrid(
        lons,
        lats,
        u,
        v,
        cycle,
        forecast_hour,
        level,
        valid_time,
        "GFS mock",
        cycle_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间"),
        valid_dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间"),
    )


def point_value(grid: WindGrid, lon: float, lat: float) -> dict[str, float]:
    """Return the nearest regular-grid wind value for a lon/lat point."""
    lon_index = int(np.abs(grid.lons - lon).argmin())
    lat_index = int(np.abs(grid.lats - lat).argmin())
    u = float(grid.u[lat_index, lon_index])
    v = float(grid.v[lat_index, lon_index])
    speed = float(np.hypot(u, v))
    direction_to = float(np.degrees(np.arctan2(u, v)) % 360)
    return {
        "lon": float(grid.lons[lon_index]),
        "lat": float(grid.lats[lat_index]),
        "u": u,
        "v": v,
        "wind_speed": speed,
        "wind_direction_to": direction_to,
        "wind_direction_from": (direction_to + 180) % 360,
    }
