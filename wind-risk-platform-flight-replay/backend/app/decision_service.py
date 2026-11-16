"""Navigation decision service built on coarse path planners.

The first version deliberately separates two thresholds:

- planner hard no-fly wind threshold: defaults to 7.9 m/s.
- navigation decision wind threshold: defaults to 5.4 m/s.

That means the planner may still find a technically passable route through
moderate wind, while the decision layer can mark it as high risk and recommend
delaying departure.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import hypot
from typing import Any, Iterable

import numpy as np

from .cost_evaluator import (
    calculate_edge_cost,
    cost_config_from_thresholds,
    decompose_wind_along_edge,
    ensure_cost_config,
)
from .grid_planner_lpa_star import LPAStarPlanner, Node
from .grid_planner_wa_lpa_star import WALPAStarPlanner
from .models import Thresholds
from .routing import plan_route
from .route_service import anchor_route_endpoints
from .wind_provider import WindGrid


DECISION_SUITABLE = "适宜通航"
DECISION_DELAY = "延后通航"
DECISION_PAUSE = "暂停通航"

RISK_LOW = "低风险"
RISK_MEDIUM = "中风险"
RISK_HIGH = "高风险"
RISK_UNAVAILABLE = "不可通航"


@dataclass(frozen=True)
class ForecastCandidate:
    """Wind/risk grids for one candidate forecast time."""

    forecast_time: str
    wind_field: WindGrid
    terrain_data: Any | None = None
    rain_data: Any | None = None
    height: str | None = None


@dataclass(frozen=True)
class DecisionConfig:
    """Business thresholds for navigation decision."""

    max_wind_speed_threshold: float = 5.4
    max_rain_threshold: float = 10.0
    min_agl_height: float = 0.0
    planner_hard_max_wind_speed: float = 7.9
    max_cumulative_cost: float | None = None
    max_headwind_threshold: float | None = None
    max_crosswind_threshold: float | None = None
    planner_type: str = "wa_lpa_star"
    thresholds: Thresholds = field(default_factory=Thresholds)
    cost_config: Any | None = None
    wa_config: Any | None = None


@dataclass
class CandidateEvaluation:
    forecast_time: str
    available: bool
    decision_feasible: bool
    decision_hint: str
    reason: str
    path: list[tuple[float, float]]
    recommended_height: str | None
    risk_level: str
    path_summary: dict[str, Any]

    def compact(self) -> dict[str, Any]:
        return {
            "forecast_time": self.forecast_time,
            "available": self.available,
            "decision_hint": self.decision_hint,
            "total_cost": self.path_summary.get("total_cost"),
            "max_wind_speed": self.path_summary.get("max_wind_speed"),
            "max_rain": self.path_summary.get("max_rain"),
            "reason": self.reason,
        }


def _candidate_from(item: Any) -> ForecastCandidate:
    if isinstance(item, ForecastCandidate):
        return item
    if isinstance(item, WindGrid):
        return ForecastCandidate(
            forecast_time=getattr(item, "valid_time", None) or f"f{getattr(item, 'forecast_hour', '')}",
            wind_field=item,
            height=getattr(item, "level", None),
        )
    if isinstance(item, dict):
        grid = item.get("wind_field", item.get("grid"))
        if grid is None:
            raise ValueError("candidate item must contain wind_field or grid")
        return ForecastCandidate(
            forecast_time=str(item.get("forecast_time") or item.get("valid_time") or getattr(grid, "valid_time", "")),
            wind_field=grid,
            terrain_data=item.get("terrain_data", item.get("terrain")),
            rain_data=item.get("rain_data", item.get("rain")),
            height=item.get("height") or getattr(grid, "level", None),
        )
    raise TypeError("candidate_forecast_times items must be ForecastCandidate, WindGrid, or dict")


def _nearest_node(grid: WindGrid, point: tuple[float, float]) -> Node:
    lon, lat = point
    return int(np.abs(grid.lats - lat).argmin()), int(np.abs(grid.lons - lon).argmin())


def _node_point(grid: WindGrid, node: Node) -> tuple[float, float]:
    row, col = node
    return float(grid.lons[col]), float(grid.lats[row])


def _endpoint_hard_wind_summary(
    grid: WindGrid,
    start_point: tuple[float, float],
    end_point: tuple[float, float],
    hard_limit_ms: float,
) -> dict[str, Any] | None:
    values = []
    blocked_names = []
    for name, point in (("起点", start_point), ("终点", end_point)):
        row, col = _nearest_node(grid, point)
        speed = float(hypot(grid.u[row, col], grid.v[row, col]))
        values.append(speed)
        if not np.isfinite(speed) or speed >= hard_limit_ms:
            blocked_names.append(name)
    if not blocked_names:
        return None
    finite_values = [value for value in values if np.isfinite(value)]
    return {
        "blocked_names": blocked_names,
        "max_wind_speed": max(finite_values) if finite_values else None,
        "reason": (
            f"{'、'.join(blocked_names)}风速达到或超过 {float(hard_limit_ms):g} m/s 硬上限，"
            "禁止通航，已跳过航线图搜索"
        ),
    }


def _nodes_from_points(grid: WindGrid, points: Iterable[tuple[float, float]]) -> list[Node]:
    nodes: list[Node] = []
    for point in points:
        node = _nearest_node(grid, point)
        if not nodes or nodes[-1] != node:
            nodes.append(node)
    return nodes


def _rain_array(rain_data: Any | None) -> np.ndarray | None:
    if rain_data is None:
        return None
    values = rain_data
    if isinstance(rain_data, dict):
        values = None
        for key in ("rain", "rain_rate", "apcp", "prate"):
            if key in rain_data:
                values = rain_data[key]
                break
        if values is None:
            return None
    array = np.asarray(values, dtype=float)
    return array if array.ndim >= 2 else None


def _terrain_array(terrain_data: Any | None) -> np.ndarray | None:
    if terrain_data is None:
        return None
    values = terrain_data
    if isinstance(terrain_data, dict):
        values = None
        for key in ("terrain", "terrain_height", "elevation", "hgt", "hgt_surface"):
            if key in terrain_data:
                values = terrain_data[key]
                break
        if values is None:
            return None
    array = np.asarray(values, dtype=float)
    return array if array.ndim >= 2 else None


def _level_height_agl(level: str | None) -> float | None:
    if not level:
        return None
    lowered = str(level).lower()
    if "agl" not in lowered:
        return None
    number = ""
    for char in lowered:
        if char.isdigit() or char == ".":
            number += char
        elif number:
            break
    return float(number) if number else None


def _planner_cost_config(config: DecisionConfig) -> dict[str, Any]:
    """Build planner config with the 7.9 m/s hard wind constraint."""

    base = cost_config_from_thresholds(config.thresholds, config.cost_config)
    return {
        "weights": asdict(base.weights),
        "thresholds": {
            **asdict(base.thresholds),
            "max_wind_speed": float(config.planner_hard_max_wind_speed),
            "max_rain": float(config.max_rain_threshold),
            "min_agl_height": float(config.min_agl_height),
        },
    }


def _plan_candidate(
    candidate: ForecastCandidate,
    start_point: tuple[float, float],
    end_point: tuple[float, float],
    config: DecisionConfig,
) -> tuple[list[tuple[float, float]], list[Node], float | None]:
    grid = candidate.wind_field
    planner_type = config.planner_type.lower().replace("-", "_")
    planner_cost_config = _planner_cost_config(config)

    if planner_type in {"astar", "a_star"}:
        result = plan_route(
            grid,
            start_point,
            end_point,
            config.thresholds,
            planner_cost_config,
            candidate.terrain_data,
            candidate.rain_data,
        )
        points = result["points"]
        return points, _nodes_from_points(grid, points), result.get("cost")

    start_node = {"lon": start_point[0], "lat": start_point[1]}
    goal_node = {"lon": end_point[0], "lat": end_point[1]}

    if planner_type in {"lpa", "lpa_star"}:
        planner = LPAStarPlanner(
            grid,
            start_node,
            goal_node,
            cost_config=planner_cost_config,
            terrain_data=candidate.terrain_data,
            rain_data=candidate.rain_data,
            thresholds=config.thresholds,
        )
    elif planner_type in {"wa_lpa", "wa_lpa_star", "walpa", "walpa_star"}:
        planner = WALPAStarPlanner(
            grid,
            start_node,
            goal_node,
            cost_config=planner_cost_config,
            terrain_data=candidate.terrain_data,
            rain_data=candidate.rain_data,
            thresholds=config.thresholds,
            wa_config=config.wa_config,
        )
    else:
        raise ValueError("planner_type must be astar, lpa_star, or wa_lpa_star")

    result = planner.plan()
    nodes = planner.node_path()
    if not nodes:
        raise ValueError("未找到可行路径")
    grid_points = [_node_point(grid, node) for node in nodes]
    return anchor_route_endpoints(grid_points, start_point, end_point), nodes, result.total_cost


def _path_summary(
    candidate: ForecastCandidate,
    path_nodes: list[Node],
    config: DecisionConfig,
    planner_total_cost: float | None,
) -> dict[str, Any]:
    grid = candidate.wind_field
    cost_config = ensure_cost_config(_planner_cost_config(config))
    rain = _rain_array(candidate.rain_data)
    terrain = _terrain_array(candidate.terrain_data)
    flight_altitude_msl = cost_config.thresholds.flight_altitude_msl
    level_agl = _level_height_agl(candidate.height or getattr(grid, "level", None))

    path_length = 0.0
    total_cost = 0.0
    max_wind = 0.0
    max_headwind = 0.0
    max_crosswind = 0.0
    max_rain = None
    min_clearance = None
    high_risk_cell_count = 0

    for node in path_nodes:
        row, col = node
        wind_speed = float(hypot(grid.u[row, col], grid.v[row, col]))
        max_wind = max(max_wind, wind_speed)
        node_rain = None if rain is None else float(rain[row, col])
        if node_rain is not None:
            max_rain = node_rain if max_rain is None else max(max_rain, node_rain)

        clearance = level_agl
        if terrain is not None and flight_altitude_msl is not None:
            clearance = float(flight_altitude_msl - terrain[row, col])
        if clearance is not None:
            min_clearance = clearance if min_clearance is None else min(min_clearance, clearance)

        if wind_speed >= config.max_wind_speed_threshold or (
            node_rain is not None and node_rain >= config.max_rain_threshold
        ):
            high_risk_cell_count += 1

    for start, end in zip(path_nodes, path_nodes[1:]):
        edge = calculate_edge_cost(start, end, grid, candidate.terrain_data, candidate.rain_data, cost_config)
        if np.isfinite(edge.distance_cost):
            path_length += edge.distance_cost
        if np.isfinite(edge.total_cost):
            total_cost += edge.total_cost

        row, col = end
        components = decompose_wind_along_edge(start, end, float(grid.u[row, col]), float(grid.v[row, col]))
        max_headwind = max(max_headwind, components["headwind"])
        max_crosswind = max(max_crosswind, components["crosswind"])

    cumulative_cost = total_cost if np.isfinite(total_cost) else planner_total_cost
    return {
        "path_length": round(path_length, 3),
        "total_cost": round(float(total_cost), 3) if np.isfinite(total_cost) else planner_total_cost,
        "max_wind_speed": round(max_wind, 3),
        "max_headwind": round(max_headwind, 3),
        "max_crosswind": round(max_crosswind, 3),
        "max_rain": None if max_rain is None else round(max_rain, 3),
        "min_agl_height": None if min_clearance is None else round(min_clearance, 3),
        "cumulative_cost": None if cumulative_cost is None else round(float(cumulative_cost), 3),
        "high_risk_cell_count": high_risk_cell_count,
    }


def _candidate_reasons(summary: dict[str, Any], config: DecisionConfig) -> tuple[bool, list[str], str]:
    reasons: list[str] = []
    feasible = True
    risk_level = RISK_LOW

    if summary["max_wind_speed"] >= config.max_wind_speed_threshold:
        feasible = False
        risk_level = RISK_HIGH
        reasons.append("当前路径最大风速超过阈值")

    if summary["max_rain"] is not None and summary["max_rain"] >= config.max_rain_threshold:
        feasible = False
        risk_level = RISK_HIGH
        reasons.append("降雨风险较高")

    if summary["min_agl_height"] is not None and summary["min_agl_height"] < config.min_agl_height:
        feasible = False
        risk_level = RISK_HIGH
        reasons.append("地形安全高度不足")

    headwind_threshold = config.max_headwind_threshold or config.max_wind_speed_threshold
    crosswind_threshold = config.max_crosswind_threshold or max(4.0, config.max_wind_speed_threshold * 0.75)
    if summary["max_headwind"] >= headwind_threshold:
        feasible = False
        risk_level = RISK_HIGH
        reasons.append("逆风风险较高")
    if summary["max_crosswind"] >= crosswind_threshold:
        feasible = False
        risk_level = RISK_HIGH
        reasons.append("侧风风险较高")

    cumulative_cost = summary.get("cumulative_cost")
    if config.max_cumulative_cost is not None and cumulative_cost is not None and cumulative_cost > config.max_cumulative_cost:
        feasible = False
        if risk_level == RISK_LOW:
            risk_level = RISK_MEDIUM
        reasons.append("累计路径代价超过设定阈值")

    if not reasons:
        reasons.append("当前时效路径满足风速、降雨、地形和代价约束")
    return feasible, reasons, risk_level


def evaluate_candidate(
    candidate: ForecastCandidate,
    start_point: tuple[float, float],
    end_point: tuple[float, float],
    config: DecisionConfig,
) -> CandidateEvaluation:
    try:
        endpoint_block = _endpoint_hard_wind_summary(
            candidate.wind_field,
            start_point,
            end_point,
            config.planner_hard_max_wind_speed,
        )
        if endpoint_block is not None:
            return CandidateEvaluation(
                forecast_time=candidate.forecast_time,
                available=True,
                decision_feasible=False,
                decision_hint="禁止通航",
                reason=endpoint_block["reason"],
                path=[],
                recommended_height=candidate.height or getattr(candidate.wind_field, "level", None),
                risk_level=RISK_UNAVAILABLE,
                path_summary={
                    "path_length": 0.0,
                    "total_cost": None,
                    "max_wind_speed": endpoint_block["max_wind_speed"],
                    "max_headwind": None,
                    "max_crosswind": None,
                    "max_rain": None,
                    "min_agl_height": None,
                    "cumulative_cost": None,
                    "high_risk_cell_count": None,
                    "planning_skipped": True,
                    "planning_skip_reason": "endpoint_wind_speed",
                },
            )
        path, path_nodes, planner_total_cost = _plan_candidate(candidate, start_point, end_point, config)
        summary = _path_summary(candidate, path_nodes, config, planner_total_cost)
        feasible, reasons, risk_level = _candidate_reasons(summary, config)
        return CandidateEvaluation(
            forecast_time=candidate.forecast_time,
            available=True,
            decision_feasible=feasible,
            decision_hint="允许通航" if feasible else "禁止通航",
            reason="；".join(reasons),
            path=path,
            recommended_height=candidate.height or getattr(candidate.wind_field, "level", None),
            risk_level=risk_level,
            path_summary=summary,
        )
    except Exception as exc:
        return CandidateEvaluation(
            forecast_time=candidate.forecast_time,
            available=False,
            decision_feasible=False,
            decision_hint="禁止通航",
            reason=f"该时效无可行路径：{exc}",
            path=[],
            recommended_height=candidate.height or getattr(candidate.wind_field, "level", None),
            risk_level=RISK_UNAVAILABLE,
            path_summary={
                "path_length": None,
                "total_cost": None,
                "max_wind_speed": None,
                "max_headwind": None,
                "max_crosswind": None,
                "max_rain": None,
                "min_agl_height": None,
                "cumulative_cost": None,
                "high_risk_cell_count": None,
            },
        )


def decide_navigation(
    start_point: tuple[float, float],
    end_point: tuple[float, float],
    candidate_forecast_times: Iterable[Any],
    *,
    max_wind_speed_threshold: float = 5.4,
    max_rain_threshold: float = 10.0,
    min_agl_height: float = 0.0,
    planner_type: str = "wa_lpa_star",
    max_cumulative_cost: float | None = None,
    max_headwind_threshold: float | None = None,
    max_crosswind_threshold: float | None = None,
    planner_hard_max_wind_speed: float = 7.9,
    cost_config: Any | None = None,
    wa_config: Any | None = None,
    thresholds: Thresholds | None = None,
) -> dict[str, Any]:
    """Evaluate current and future forecast candidates and return a decision."""

    candidates = [_candidate_from(item) for item in candidate_forecast_times]
    if not candidates:
        raise ValueError("candidate_forecast_times cannot be empty")

    config = DecisionConfig(
        max_wind_speed_threshold=max_wind_speed_threshold,
        max_rain_threshold=max_rain_threshold,
        min_agl_height=min_agl_height,
        planner_hard_max_wind_speed=planner_hard_max_wind_speed,
        max_cumulative_cost=max_cumulative_cost,
        max_headwind_threshold=max_headwind_threshold,
        max_crosswind_threshold=max_crosswind_threshold,
        planner_type=planner_type,
        thresholds=thresholds or Thresholds(),
        cost_config=cost_config,
        wa_config=wa_config,
    )

    evaluations = [evaluate_candidate(candidate, start_point, end_point, config) for candidate in candidates]
    current = evaluations[0]
    feasible = [item for item in evaluations if item.decision_feasible]
    best = feasible[0] if feasible else next((item for item in evaluations if item.available), current)

    if current.decision_feasible:
        decision = DECISION_SUITABLE
        recommended = current
        reason = current.reason
    elif feasible:
        decision = DECISION_DELAY
        recommended = feasible[0]
        reason = f"当前时效风险较高或不可行；未来 {recommended.forecast_time} 存在可行路径"
    else:
        decision = DECISION_PAUSE
        recommended = best
        if any(item.available for item in evaluations):
            reason = "所有候选时效均未满足通航阈值要求"
        else:
            reason = "所有候选时效均无可行路径"

    return {
        "decision": decision,
        "navigation_allowed": current.decision_feasible,
        "navigation_decision": "允许通航" if current.decision_feasible else "禁止通航",
        "recommended_start_time": recommended.forecast_time if decision != DECISION_PAUSE else None,
        "recommended_height": recommended.recommended_height,
        "best_path": recommended.path if decision != DECISION_PAUSE else [],
        "risk_level": recommended.risk_level if decision != DECISION_PAUSE else RISK_UNAVAILABLE,
        "reason": reason,
        "path_summary": recommended.path_summary,
        "candidate_results": [item.compact() for item in evaluations],
    }
