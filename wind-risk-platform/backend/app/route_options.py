"""Route-planning user options and strategy-to-cost mapping.

The frontend exposes friendly choices such as "避风优先"; this module keeps the
actual planner-facing values explicit and shared by A*, LPA* and WA-LPA*.
"""

from __future__ import annotations

import os
from typing import Any


AIRCRAFT_FIXED_WING = "fixed_wing"

AIRCRAFT_ALIASES = {
    "fixed_wing": AIRCRAFT_FIXED_WING,
    "fixed-wing": AIRCRAFT_FIXED_WING,
    "fixedwing": AIRCRAFT_FIXED_WING,
    "固定翼": AIRCRAFT_FIXED_WING,
    "固定翼无人机": AIRCRAFT_FIXED_WING,
}

PLANNING_STRATEGY_DISTANCE = "distance_priority"
PLANNING_STRATEGY_BALANCED = "balanced"
PLANNING_STRATEGY_WIND_AVOIDANCE = "wind_avoidance"

STRATEGY_ALIASES = {
    "distance_priority": PLANNING_STRATEGY_DISTANCE,
    "distance": PLANNING_STRATEGY_DISTANCE,
    "shortest": PLANNING_STRATEGY_DISTANCE,
    "route_first": PLANNING_STRATEGY_DISTANCE,
    "路程优先": PLANNING_STRATEGY_DISTANCE,
    "距离优先": PLANNING_STRATEGY_DISTANCE,
    "balanced": PLANNING_STRATEGY_BALANCED,
    "balance": PLANNING_STRATEGY_BALANCED,
    "均衡": PLANNING_STRATEGY_BALANCED,
    "综合均衡": PLANNING_STRATEGY_BALANCED,
    "wind_avoidance": PLANNING_STRATEGY_WIND_AVOIDANCE,
    "wind_priority": PLANNING_STRATEGY_WIND_AVOIDANCE,
    "wind": PLANNING_STRATEGY_WIND_AVOIDANCE,
    "avoid_wind": PLANNING_STRATEGY_WIND_AVOIDANCE,
    "风速优先": PLANNING_STRATEGY_WIND_AVOIDANCE,
    "避风优先": PLANNING_STRATEGY_WIND_AVOIDANCE,
}


def normalize_aircraft_model(value: str | None) -> str:
    """Normalize the user-facing aircraft model to a stable backend value."""

    if not value:
        return AIRCRAFT_FIXED_WING
    key = str(value).strip().lower().replace(" ", "_")
    normalized = AIRCRAFT_ALIASES.get(key) or AIRCRAFT_ALIASES.get(str(value).strip())
    if normalized is None:
        raise ValueError("aircraft_model 当前仅支持 fixed_wing / 固定翼无人机")
    return normalized


def normalize_planning_strategy(value: str | None) -> str:
    """Normalize the route strategy value."""

    if not value:
        return PLANNING_STRATEGY_WIND_AVOIDANCE
    key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    normalized = STRATEGY_ALIASES.get(key) or STRATEGY_ALIASES.get(str(value).strip())
    if normalized is None:
        raise ValueError("planning_strategy must be distance_priority, balanced, or wind_avoidance")
    return normalized


def build_strategy_cost_config(strategy: str | None, base_config: Any | None = None) -> dict[str, Any]:
    """Return cost-config overrides for the selected planning strategy.

    Distance-priority keeps the older shorter-route behavior.
    Balanced increases high-wind sensitivity without turning every small detour
    into a long bypass. Wind avoidance strongly weights average/high grid wind
    risk and almost removes headwind/crosswind preference so the route is driven
    mainly by low-wind corridors.
    """

    strategy = normalize_planning_strategy(strategy)
    config: dict[str, Any] = {}
    if isinstance(base_config, dict):
        config = {
            "weights": dict(base_config.get("weights", {})),
            "thresholds": dict(base_config.get("thresholds", {})),
        }

    weights = dict(config.get("weights", {}))
    if strategy == PLANNING_STRATEGY_DISTANCE:
        # Current/default behavior.
        weights.update(
            {
                "alpha_distance": 1.0,
                "beta_wind": 12.0,
                "gamma_headwind": 0.7,
                "delta_crosswind": 0.2,
            }
        )
    elif strategy == PLANNING_STRATEGY_BALANCED:
        weights.update(
            {
                "alpha_distance": 0.9,
                "beta_wind": 36.0,
                "gamma_headwind": 0.25,
                "delta_crosswind": 0.12,
            }
        )
    elif strategy == PLANNING_STRATEGY_WIND_AVOIDANCE:
        weights.update(
            {
                "alpha_distance": 0.35,
                "beta_wind": 220.0,
                "gamma_headwind": 0.03,
                "delta_crosswind": 0.03,
            }
        )
    config["weights"] = weights
    config.setdefault("thresholds", {})
    return config


def route_search_padding_deg(start: tuple[float, float], end: tuple[float, float], strategy: str | None = None) -> float:
    """Return bbox padding in degrees for route planning.

    A larger padded rectangle lets the grid search discover low-wind bypasses
    away from the straight line. Wind-avoidance gets the widest corridor.
    """

    def _float_env(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except ValueError:
            return default

    lon_span = abs(float(end[0]) - float(start[0]))
    lat_span = abs(float(end[1]) - float(start[1]))
    span = max(lon_span, lat_span)
    strategy = normalize_planning_strategy(strategy)
    if strategy == PLANNING_STRATEGY_WIND_AVOIDANCE:
        min_padding = _float_env("ROUTE_PLAN_WIND_MIN_PADDING_DEG", 1.5)
        max_padding = _float_env("ROUTE_PLAN_WIND_MAX_PADDING_DEG", 6.0)
        factor = _float_env("ROUTE_PLAN_WIND_PADDING_FACTOR", 1.25)
        return max(min_padding, min(max_padding, span * factor))
    if strategy == PLANNING_STRATEGY_BALANCED:
        return max(0.8, min(4.0, span * 0.9))
    return max(0.6, min(2.5, span * 0.65))


def describe_strategy(strategy: str | None) -> str:
    strategy = normalize_planning_strategy(strategy)
    return {
        PLANNING_STRATEGY_DISTANCE: "路程优先",
        PLANNING_STRATEGY_BALANCED: "均衡避险",
        PLANNING_STRATEGY_WIND_AVOIDANCE: "避风优先",
    }[strategy]


def describe_aircraft_model(model: str | None) -> str:
    model = normalize_aircraft_model(model)
    return {AIRCRAFT_FIXED_WING: "固定翼无人机"}[model]
