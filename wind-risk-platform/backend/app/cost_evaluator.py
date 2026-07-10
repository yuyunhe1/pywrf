"""Reusable edge-cost evaluation for grid-based route planners.

The functions in this module are deliberately independent from A* so future
D* Lite / WA-LPA* implementations can call the same node flyability and edge
cost logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import asin, cos, hypot, radians, sin, sqrt
from typing import Any

import numpy as np


EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class CostWeights:
    alpha_distance: float = 1.0
    beta_wind: float = 12.0
    gamma_headwind: float = 0.7
    delta_crosswind: float = 0.2
    eta_terrain: float = 5.0
    mu_rain: float = 8.0


@dataclass(frozen=True)
class CostThresholds:
    safe_wind_speed: float = 1.5
    max_wind_speed: float = 7.9
    max_rain: float = 10.0
    rain_blocking: bool = False
    min_agl_height: float = 0.0
    flight_altitude_msl: float | None = None


@dataclass(frozen=True)
class CostConfig:
    weights: CostWeights = field(default_factory=CostWeights)
    thresholds: CostThresholds = field(default_factory=CostThresholds)


@dataclass(frozen=True)
class EdgeCost:
    distance_cost: float
    wind_speed_cost: float
    headwind_cost: float
    tailwind: float
    crosswind_cost: float
    terrain_cost: float
    rain_cost: float
    total_cost: float
    blocked: bool = False
    reason: str | None = None

    def __float__(self) -> float:
        return self.total_cost

    def as_dict(self) -> dict[str, float | bool | str | None]:
        return asdict(self)


def normalize_risk(value: float, safe_value: float, limit_value: float) -> float:
    """Normalize a risk variable to [0, 1] between safe and limit values."""
    if not np.isfinite(value):
        return 1.0
    if limit_value <= safe_value:
        return 1.0 if value >= limit_value else 0.0
    if value <= safe_value:
        return 0.0
    if value >= limit_value:
        return 1.0
    return float((value - safe_value) / (limit_value - safe_value))


def cost_config_from_thresholds(thresholds: Any | None = None, config: Any | None = None) -> CostConfig:
    """Build a reusable cost config from API wind thresholds plus optional overrides.

    Existing API thresholds use safe/notice/warning/danger. The hard wind limit
    intentionally defaults to danger (7.9 m/s), not warning (5.4 m/s).
    """
    base = CostConfig()
    if thresholds is not None:
        base = CostConfig(
            weights=base.weights,
            thresholds=CostThresholds(
                safe_wind_speed=float(getattr(thresholds, "safe", base.thresholds.safe_wind_speed)),
                max_wind_speed=float(getattr(thresholds, "danger", base.thresholds.max_wind_speed)),
                max_rain=base.thresholds.max_rain,
                rain_blocking=base.thresholds.rain_blocking,
                min_agl_height=base.thresholds.min_agl_height,
                flight_altitude_msl=base.thresholds.flight_altitude_msl,
            ),
        )
    if config is None:
        return base
    if isinstance(config, dict):
        return CostConfig(
            weights=CostWeights(**{**asdict(base.weights), **config.get("weights", {})}),
            thresholds=CostThresholds(**{**asdict(base.thresholds), **config.get("thresholds", {})}),
        )
    return ensure_cost_config(config)


def ensure_cost_config(config: Any | None = None) -> CostConfig:
    if config is None:
        return CostConfig()
    if isinstance(config, CostConfig):
        return config
    if isinstance(config, dict):
        weights = config.get("weights", {})
        thresholds = config.get("thresholds", {})
        return CostConfig(
            weights=CostWeights(**{**asdict(CostWeights()), **weights}),
            thresholds=CostThresholds(**{**asdict(CostThresholds()), **thresholds}),
        )
    return config


def _node_row_col(node: Any) -> tuple[int, int]:
    if isinstance(node, dict):
        row = node.get("row", node.get("r", node.get("y")))
        col = node.get("col", node.get("c", node.get("x")))
        if row is None or col is None:
            raise ValueError("node dict must contain row/col, r/c, or y/x grid indices")
        return int(row), int(col)
    if hasattr(node, "row") and hasattr(node, "col"):
        return int(node.row), int(node.col)
    return int(node[0]), int(node[1])


def _cell_point(wind_field, node: Any) -> tuple[float, float]:
    row, col = _node_row_col(node)
    return float(wind_field.lons[col]), float(wind_field.lats[row])


def _haversine_km(start: tuple[float, float], end: tuple[float, float]) -> float:
    lon1, lat1 = map(radians, start)
    lon2, lat2 = map(radians, end)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def _grid_value(data: Any, node: Any, keys: tuple[str, ...]) -> float | None:
    if data is None:
        return None
    row, col = _node_row_col(node)
    values = data
    if isinstance(data, dict):
        values = None
        for key in keys:
            if key in data:
                values = data[key]
                break
        if values is None:
            return None
    values = np.asarray(values)
    if values.ndim < 2:
        return None
    return float(values[row, col])


def _wind_at(wind_field, node: Any) -> tuple[float, float, float]:
    row, col = _node_row_col(node)
    u = float(wind_field.u[row, col])
    v = float(wind_field.v[row, col])
    return u, v, float(hypot(u, v))


def _level_height_agl(wind_field) -> float | None:
    level = str(getattr(wind_field, "level", "")).lower()
    if "agl" not in level:
        return None
    number = ""
    for char in level:
        if char.isdigit() or char == ".":
            number += char
        elif number:
            break
    return float(number) if number else None


def _clearance_agl(node: Any, wind_field, terrain_data: Any, config: CostConfig) -> float | None:
    terrain_height = _grid_value(terrain_data, node, ("terrain", "terrain_height", "elevation", "hgt", "hgt_surface"))
    if terrain_height is not None and config.thresholds.flight_altitude_msl is not None:
        return float(config.thresholds.flight_altitude_msl - terrain_height)
    if isinstance(node, dict) and "altitude_msl" in node and terrain_height is not None:
        return float(node["altitude_msl"] - terrain_height)
    if isinstance(node, dict) and "agl_height" in node:
        return float(node["agl_height"])
    return _level_height_agl(wind_field)


def decompose_wind_along_edge(node_from: Any, node_to: Any, u: float, v: float) -> dict[str, float]:
    """Return wind components relative to movement direction.

    u is positive eastward and v is positive northward. Grid row increases
    southward, so the north component of a row/column edge is -dr.
    """
    from_row, from_col = _node_row_col(node_from)
    to_row, to_col = _node_row_col(node_to)
    east = float(to_col - from_col)
    north = float(-(to_row - from_row))
    length = hypot(east, north)
    if length == 0:
        return {"headwind": 0.0, "tailwind": 0.0, "crosswind": 0.0}
    east /= length
    north /= length
    along = u * east + v * north
    cross = abs(u * north - v * east)
    return {
        "headwind": float(max(0.0, -along)),
        "tailwind": float(max(0.0, along)),
        "crosswind": float(cross),
    }


def is_node_flyable(node: Any, wind_field, terrain_data: Any, rain_data: Any, config: Any | None) -> bool:
    config = ensure_cost_config(config)
    _, _, wind_speed = _wind_at(wind_field, node)
    if not np.isfinite(wind_speed) or wind_speed >= config.thresholds.max_wind_speed:
        return False

    min_agl = config.thresholds.min_agl_height
    clearance = _clearance_agl(node, wind_field, terrain_data, config)
    if min_agl > 0 and clearance is not None and clearance < min_agl:
        return False

    rain = _grid_value(rain_data, node, ("rain", "rain_rate", "apcp", "prate"))
    if rain is not None and config.thresholds.rain_blocking and rain >= config.thresholds.max_rain:
        return False
    return True


def calculate_edge_cost(node_from: Any, node_to: Any, wind_field, terrain_data: Any, rain_data: Any, config: Any | None) -> EdgeCost:
    config = ensure_cost_config(config)
    if not is_node_flyable(node_to, wind_field, terrain_data, rain_data, config):
        return EdgeCost(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, float("inf"), True, "node is not flyable")

    start = _cell_point(wind_field, node_from)
    end = _cell_point(wind_field, node_to)
    distance_cost = _haversine_km(start, end)

    u, v, wind_speed = _wind_at(wind_field, node_to)
    wind_speed_cost = normalize_risk(
        wind_speed,
        config.thresholds.safe_wind_speed,
        config.thresholds.max_wind_speed,
    )
    components = decompose_wind_along_edge(node_from, node_to, u, v)
    headwind_cost = components["headwind"]
    crosswind_cost = normalize_risk(components["crosswind"], 0.0, config.thresholds.max_wind_speed)

    clearance = _clearance_agl(node_to, wind_field, terrain_data, config)
    terrain_cost = 0.0
    if clearance is not None and config.thresholds.min_agl_height > 0:
        safe_clearance = config.thresholds.min_agl_height * 2
        terrain_cost = normalize_risk(safe_clearance - clearance, 0.0, config.thresholds.min_agl_height)

    rain = _grid_value(rain_data, node_to, ("rain", "rain_rate", "apcp", "prate"))
    rain_cost = 0.0 if rain is None else normalize_risk(float(rain), 0.0, config.thresholds.max_rain)

    weights = config.weights
    total = (
        weights.alpha_distance * distance_cost
        + weights.beta_wind * wind_speed_cost
        + weights.gamma_headwind * headwind_cost
        + weights.delta_crosswind * crosswind_cost
        + weights.eta_terrain * terrain_cost
        + weights.mu_rain * rain_cost
    )
    return EdgeCost(
        distance_cost=distance_cost,
        wind_speed_cost=wind_speed_cost,
        headwind_cost=headwind_cost,
        tailwind=components["tailwind"],
        crosswind_cost=crosswind_cost,
        terrain_cost=terrain_cost,
        rain_cost=rain_cost,
        total_cost=float(total),
    )
