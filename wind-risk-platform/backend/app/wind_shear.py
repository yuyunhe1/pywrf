"""Reusable vector wind-shear calculation, constraints and route statistics."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .route_service import haversine_km


SHEAR_SAFE = "安全"
SHEAR_CAUTION = "谨慎"
SHEAR_NO_FLY = "禁飞"
SHEAR_MISSING = "缺测"


@dataclass(frozen=True)
class VerticalWindShearConfig:
    # Retained for request/config compatibility.  The current planners do not
    # evaluate vertical shear and never use it as a node constraint.
    enabled: bool = False
    caution_delta_v_10m_ms: float = 1.0
    hard_delta_v_10m_ms: float = 3.0
    hard_delta_v_30m_ms: float = 6.0
    caution_direction_change_deg: float = 20.0
    hard_direction_change_deg: float = 45.0
    hard_constraint_enabled: bool = False


@dataclass(frozen=True)
class HorizontalWindShearConfig:
    enabled: bool = True
    hard_delta_v_1km_ms: float = 2.6
    hard_direction_change_deg: float = 45.0
    hard_constraint_enabled: bool = True


@dataclass(frozen=True)
class WindShearConfig:
    enabled: bool = True
    min_wind_speed_for_direction_ms: float = 0.5
    vertical: VerticalWindShearConfig = VerticalWindShearConfig()
    horizontal: HorizontalWindShearConfig = HorizontalWindShearConfig()
    note: str = "项目实验性风险阈值，可根据观测和实验结果调整，不代表无人机国家强制标准"


@dataclass(frozen=True)
class VerticalWindShearField:
    status: str
    lower_height_m: float | None = None
    upper_height_m: float | None = None
    vertical_shear_s1: np.ndarray | None = None
    delta_wind_vector_ms: np.ndarray | None = None
    delta_v_10m_ms: np.ndarray | None = None
    delta_v_30m_ms: np.ndarray | None = None
    direction_change_deg: np.ndarray | None = None
    direction_valid: np.ndarray | None = None
    shear_level: np.ndarray | None = None
    is_flyable: np.ndarray | None = None
    valid_mask: np.ndarray | None = None
    missing_reason: str | None = None


@dataclass(frozen=True)
class WindShearEnvironment:
    config: WindShearConfig
    vertical: VerticalWindShearField | None = None


class WindShearBlockedError(ValueError):
    """Raised when explored routes are blocked by horizontal-shear edges."""

    def __init__(self, blocked_count: int = 1):
        self.blocked_count = max(1, int(blocked_count))
        super().__init__("规划区域内存在超过阈值的水平风切变边，当前条件下无可行路径。")


_BUILTIN_DEFAULT = {
    "enabled": True,
    "min_wind_speed_for_direction_ms": 0.5,
    "vertical": asdict(VerticalWindShearConfig()),
    "horizontal": asdict(HorizontalWindShearConfig()),
    "note": WindShearConfig().note,
}


def _load_default_mapping() -> dict[str, Any]:
    configured = os.getenv("WIND_SHEAR_CONFIG_FILE")
    path = Path(configured) if configured else Path(__file__).resolve().parents[1] / "config" / "wind_shear.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else dict(_BUILTIN_DEFAULT)
    except (OSError, ValueError, TypeError):
        return dict(_BUILTIN_DEFAULT)


DEFAULT_WIND_SHEAR_CONFIG = _load_default_mapping()


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise ValueError("wind_shear config must be a mapping")


def ensure_wind_shear_config(value: Any | None = None) -> WindShearConfig:
    source = _mapping(value)
    vertical_source = {**DEFAULT_WIND_SHEAR_CONFIG.get("vertical", {}), **_mapping(source.get("vertical"))}
    horizontal_source = {**DEFAULT_WIND_SHEAR_CONFIG.get("horizontal", {}), **_mapping(source.get("horizontal"))}
    merged = {**DEFAULT_WIND_SHEAR_CONFIG, **source}
    vertical = VerticalWindShearConfig(**vertical_source)
    horizontal = HorizontalWindShearConfig(**horizontal_source)
    config = WindShearConfig(
        enabled=bool(merged.get("enabled", True)),
        min_wind_speed_for_direction_ms=float(merged.get("min_wind_speed_for_direction_ms", 0.5)),
        vertical=vertical,
        horizontal=horizontal,
        note=str(merged.get("note", _BUILTIN_DEFAULT["note"])),
    )
    validate_wind_shear_config(config)
    return config


def validate_wind_shear_config(config: WindShearConfig) -> None:
    values = [
        config.min_wind_speed_for_direction_ms,
        config.vertical.caution_delta_v_10m_ms,
        config.vertical.hard_delta_v_10m_ms,
        config.vertical.hard_delta_v_30m_ms,
        config.vertical.caution_direction_change_deg,
        config.vertical.hard_direction_change_deg,
        config.horizontal.hard_delta_v_1km_ms,
        config.horizontal.hard_direction_change_deg,
    ]
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("风切变阈值必须是非负有限数值")
    if config.vertical.caution_delta_v_10m_ms >= config.vertical.hard_delta_v_10m_ms:
        raise ValueError("垂直风切变谨慎阈值必须小于硬约束阈值")
    if config.vertical.caution_direction_change_deg >= config.vertical.hard_direction_change_deg:
        raise ValueError("垂直风向谨慎阈值必须小于硬约束阈值")
    if config.vertical.hard_delta_v_10m_ms <= 0 or config.vertical.hard_delta_v_30m_ms <= 0:
        raise ValueError("垂直风切变硬约束阈值必须大于0")
    if config.horizontal.hard_delta_v_1km_ms <= 0 or config.horizontal.hard_direction_change_deg <= 0:
        raise ValueError("水平风切变硬约束阈值必须大于0")


def compute_direction_change(
    u1: float,
    v1: float,
    u2: float,
    v2: float,
    min_wind_speed_for_direction: float = 0.5,
) -> float | None:
    values = (u1, v1, u2, v2)
    if any(not math.isfinite(float(value)) for value in values):
        return None
    speed1 = math.hypot(float(u1), float(v1))
    speed2 = math.hypot(float(u2), float(v2))
    if speed1 < min_wind_speed_for_direction or speed2 < min_wind_speed_for_direction:
        return None
    cosine = (float(u1) * float(u2) + float(v1) * float(v2)) / (speed1 * speed2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _missing_result(kind: str, reason: str) -> dict[str, Any]:
    prefix = "vertical" if kind == "vertical" else "horizontal"
    return {
        "status": "missing",
        f"{prefix}_shear_s1": None,
        "delta_wind_vector_ms": None,
        "delta_v_10m_ms": None,
        "delta_v_30m_ms": None,
        "horizontal_distance_m": None,
        "delta_v_1km_ms": None,
        "direction_change_deg": None,
        "direction_is_valid": False,
        "shear_level": SHEAR_MISSING,
        "is_flyable": None,
        "blocked_reason": None,
        "missing_reason": reason,
    }


def compute_vertical_wind_shear(
    u_lower: float,
    v_lower: float,
    z_lower: float,
    u_upper: float,
    v_upper: float,
    z_upper: float,
    config: Any | None = None,
) -> dict[str, Any]:
    cfg = ensure_wind_shear_config(config)
    values = (u_lower, v_lower, z_lower, u_upper, v_upper, z_upper)
    if any(not math.isfinite(float(value)) for value in values):
        return _missing_result("vertical", "上下层风矢量或高度缺测")
    height_difference = abs(float(z_upper) - float(z_lower))
    if height_difference <= 0:
        return _missing_result("vertical", "上下层实际高度差必须大于0")
    delta = math.hypot(float(u_upper) - float(u_lower), float(v_upper) - float(v_lower))
    shear = delta / height_difference
    delta10 = shear * 10.0
    delta30 = shear * 30.0
    direction = compute_direction_change(
        u_lower,
        v_lower,
        u_upper,
        v_upper,
        cfg.min_wind_speed_for_direction_ms,
    )
    vertical = cfg.vertical
    hard = delta10 >= vertical.hard_delta_v_10m_ms or delta30 > vertical.hard_delta_v_30m_ms
    if direction is not None:
        hard = hard or direction > vertical.hard_direction_change_deg
    caution = delta10 >= vertical.caution_delta_v_10m_ms
    if direction is not None:
        caution = caution or direction >= vertical.caution_direction_change_deg
    level = SHEAR_NO_FLY if hard else SHEAR_CAUTION if caution else SHEAR_SAFE
    flyable = not (cfg.enabled and vertical.enabled and vertical.hard_constraint_enabled and hard)
    return {
        "status": "valid",
        "vertical_shear_s1": shear,
        "delta_wind_vector_ms": delta,
        "delta_v_10m_ms": delta10,
        "delta_v_30m_ms": delta30,
        "direction_change_deg": direction,
        "direction_is_valid": direction is not None,
        "shear_level": level,
        "is_flyable": flyable,
        "blocked_reason": None if flyable else "vertical_wind_shear",
        "lower_height_m": float(z_lower),
        "upper_height_m": float(z_upper),
    }


def _point(node: Any) -> tuple[float, float]:
    if isinstance(node, dict):
        return float(node.get("lon", node.get("longitude"))), float(node.get("lat", node.get("latitude")))
    return float(node[0]), float(node[1])


def _point_agl_height(node: Any) -> float | None:
    if not isinstance(node, dict):
        return None
    value = node.get("altitude_agl_m", node.get("agl_height", node.get("agl_m")))
    if value is None:
        return None
    height = float(value)
    return height if math.isfinite(height) else None


def _point_wind(node: Any, wind_field: Any, row: int, col: int) -> tuple[float, float]:
    if isinstance(node, dict):
        u_value = node.get("u_mps", node.get("u"))
        v_value = node.get("v_mps", node.get("v"))
        if u_value is not None and v_value is not None:
            u = float(u_value)
            v = float(v_value)
            if math.isfinite(u) and math.isfinite(v):
                return u, v
    return float(wind_field.u[row, col]), float(wind_field.v[row, col])


def compute_horizontal_wind_shear(
    node_from: Any,
    node_to: Any,
    u_from: float,
    v_from: float,
    u_to: float,
    v_to: float,
    config: Any | None = None,
) -> dict[str, Any]:
    cfg = ensure_wind_shear_config(config)
    values = (u_from, v_from, u_to, v_to)
    if any(not math.isfinite(float(value)) for value in values):
        return _missing_result("horizontal", "航段端点风矢量缺测")
    distance_m = haversine_km(_point(node_from), _point(node_to)) * 1000.0
    if not math.isfinite(distance_m) or distance_m <= 0:
        return _missing_result("horizontal", "航段实际水平距离必须大于0")
    delta = math.hypot(float(u_to) - float(u_from), float(v_to) - float(v_from))
    shear = delta / distance_m
    delta1km = shear * 1000.0
    direction = compute_direction_change(
        u_from,
        v_from,
        u_to,
        v_to,
        cfg.min_wind_speed_for_direction_ms,
    )
    horizontal = cfg.horizontal
    hard = delta1km >= horizontal.hard_delta_v_1km_ms
    if direction is not None:
        hard = hard or direction >= horizontal.hard_direction_change_deg
    level = SHEAR_NO_FLY if hard else SHEAR_SAFE
    flyable = not (cfg.enabled and horizontal.enabled and horizontal.hard_constraint_enabled and hard)
    return {
        "status": "valid",
        "horizontal_shear_s1": shear,
        "horizontal_distance_m": distance_m,
        "delta_wind_vector_ms": delta,
        "delta_v_1km_ms": delta1km,
        "direction_change_deg": direction,
        "direction_is_valid": direction is not None,
        "shear_level": level,
        "is_flyable": flyable,
        "blocked_reason": None if flyable else "horizontal_wind_shear",
    }


def level_height_m(level: Any) -> float | None:
    text = str(level).lower().replace("agl", "").replace(" ", "")
    number = ""
    for char in text:
        if char.isdigit() or char == ".":
            number += char
        elif number:
            break
    return float(number) if number else None


def select_vertical_layer_pair(grids: Iterable[Any], target_height_m: float) -> tuple[Any, Any] | None:
    valid = sorted(
        ((height, grid) for grid in grids if (height := level_height_m(getattr(grid, "level", ""))) is not None),
        key=lambda item: item[0],
    )
    if len(valid) < 2:
        return None
    heights = [item[0] for item in valid]
    insertion = int(np.searchsorted(np.asarray(heights), target_height_m))
    exact = insertion < len(heights) and math.isclose(heights[insertion], target_height_m, abs_tol=1e-6)
    if exact and 0 < insertion < len(valid) - 1:
        return valid[insertion - 1][1], valid[insertion + 1][1]
    if insertion <= 0:
        return valid[0][1], valid[1][1]
    if insertion >= len(valid):
        return valid[-2][1], valid[-1][1]
    return valid[insertion - 1][1], valid[insertion][1]


def build_vertical_wind_shear_field(
    grids: Iterable[Any],
    target_height_m: float,
    config: Any | None = None,
) -> VerticalWindShearField:
    cfg = ensure_wind_shear_config(config)
    pair = select_vertical_layer_pair(grids, target_height_m)
    if pair is None:
        return VerticalWindShearField(status="missing", missing_reason="至少需要两个有效高度层")
    lower, upper = pair
    lower_height = level_height_m(lower.level)
    upper_height = level_height_m(upper.level)
    if lower_height is None or upper_height is None or math.isclose(lower_height, upper_height):
        return VerticalWindShearField(status="missing", missing_reason="上下层实际高度无效")
    if lower.u.shape != upper.u.shape or not np.allclose(lower.lons, upper.lons) or not np.allclose(lower.lats, upper.lats):
        return VerticalWindShearField(status="missing", missing_reason="上下层经纬度网格不一致")

    u_lower = np.asarray(lower.u, dtype=float)
    v_lower = np.asarray(lower.v, dtype=float)
    u_upper = np.asarray(upper.u, dtype=float)
    v_upper = np.asarray(upper.v, dtype=float)
    valid = np.isfinite(u_lower) & np.isfinite(v_lower) & np.isfinite(u_upper) & np.isfinite(v_upper)
    height_difference = abs(upper_height - lower_height)
    delta = np.hypot(u_upper - u_lower, v_upper - v_lower)
    shear = delta / height_difference
    delta10 = shear * 10.0
    delta30 = shear * 30.0

    speed_lower = np.hypot(u_lower, v_lower)
    speed_upper = np.hypot(u_upper, v_upper)
    direction_valid = valid & (speed_lower >= cfg.min_wind_speed_for_direction_ms) & (speed_upper >= cfg.min_wind_speed_for_direction_ms)
    direction = np.full(delta.shape, np.nan, dtype=float)
    denominator = speed_lower * speed_upper
    cosine = np.zeros(delta.shape, dtype=float)
    np.divide(u_lower * u_upper + v_lower * v_upper, denominator, out=cosine, where=direction_valid)
    direction[direction_valid] = np.degrees(np.arccos(np.clip(cosine[direction_valid], -1.0, 1.0)))

    vertical = cfg.vertical
    hard = (delta10 >= vertical.hard_delta_v_10m_ms) | (delta30 > vertical.hard_delta_v_30m_ms)
    hard |= direction_valid & (direction > vertical.hard_direction_change_deg)
    caution = (delta10 >= vertical.caution_delta_v_10m_ms) | (direction_valid & (direction >= vertical.caution_direction_change_deg))
    levels = np.full(delta.shape, SHEAR_SAFE, dtype=object)
    levels[caution] = SHEAR_CAUTION
    levels[hard] = SHEAR_NO_FLY
    levels[~valid] = SHEAR_MISSING
    flyable = np.ones(delta.shape, dtype=bool)
    if cfg.enabled and vertical.enabled and vertical.hard_constraint_enabled:
        flyable[hard & valid] = False
    return VerticalWindShearField(
        status="valid",
        lower_height_m=lower_height,
        upper_height_m=upper_height,
        vertical_shear_s1=shear,
        delta_wind_vector_ms=delta,
        delta_v_10m_ms=delta10,
        delta_v_30m_ms=delta30,
        direction_change_deg=direction,
        direction_valid=direction_valid,
        shear_level=levels,
        is_flyable=flyable,
        valid_mask=valid,
    )


def thin_vertical_wind_shear_field(field: VerticalWindShearField, stride: int) -> VerticalWindShearField:
    if stride <= 1 or field.status != "valid":
        return field
    values = asdict(field)
    for name in (
        "vertical_shear_s1",
        "delta_wind_vector_ms",
        "delta_v_10m_ms",
        "delta_v_30m_ms",
        "direction_change_deg",
        "direction_valid",
        "shear_level",
        "is_flyable",
        "valid_mask",
    ):
        array = getattr(field, name)
        values[name] = None if array is None else array[::stride, ::stride]
    return VerticalWindShearField(**values)


def vertical_shear_at(field: VerticalWindShearField | None, row: int, col: int) -> dict[str, Any]:
    if field is None or field.status != "valid" or field.valid_mask is None:
        reason = None if field is None else field.missing_reason
        return _missing_result("vertical", reason or "没有可用的上下相邻高度层")
    if not bool(field.valid_mask[row, col]):
        return _missing_result("vertical", "该格点上下层风矢量缺测")
    direction = float(field.direction_change_deg[row, col]) if bool(field.direction_valid[row, col]) else None
    flyable = bool(field.is_flyable[row, col])
    return {
        "status": "valid",
        "vertical_shear_s1": float(field.vertical_shear_s1[row, col]),
        "delta_wind_vector_ms": float(field.delta_wind_vector_ms[row, col]),
        "delta_v_10m_ms": float(field.delta_v_10m_ms[row, col]),
        "delta_v_30m_ms": float(field.delta_v_30m_ms[row, col]),
        "direction_change_deg": direction,
        "direction_is_valid": direction is not None,
        "shear_level": str(field.shear_level[row, col]),
        "is_flyable": flyable,
        "blocked_reason": None if flyable else "vertical_wind_shear",
        "lower_height_m": field.lower_height_m,
        "upper_height_m": field.upper_height_m,
    }


def node_vertical_shear(node: Any, environment: WindShearEnvironment | None) -> dict[str, Any]:
    if environment is None:
        return _missing_result("vertical", "未提供垂直风切变环境")
    if isinstance(node, dict):
        row = int(node.get("row", node.get("r", node.get("y"))))
        col = int(node.get("col", node.get("c", node.get("x"))))
    else:
        row, col = int(node[0]), int(node[1])
    return vertical_shear_at(environment.vertical, row, col)


def analyze_route_wind_shear(
    points: list[Any],
    wind_field: Any,
    environment: WindShearEnvironment,
) -> dict[str, Any]:
    """Analyze horizontal shear on the route's consecutive horizontal edges.

    Vertical shear fields remain available as low-level utilities for future
    work, but are deliberately excluded from route planning and route risk.
    """

    node_values: list[tuple[tuple[float, float], int, int, float, float, float | None]] = []
    for point_value in points:
        point = _point(point_value)
        row = int(np.abs(wind_field.lats - point[1]).argmin())
        col = int(np.abs(wind_field.lons - point[0]).argmin())
        u, v = _point_wind(point_value, wind_field, row, col)
        node_values.append(
            (point, row, col, u, v, _point_agl_height(point_value))
        )

    horizontal_results: list[tuple[int, tuple[float, float], dict[str, Any]]] = []
    for index, (left, right) in enumerate(zip(node_values, node_values[1:])):
        if left[5] is not None and right[5] is not None and not math.isclose(left[5], right[5]):
            # This is an altitude transition, not an edge between adjacent
            # horizontal nodes on one layer.
            continue
        result = compute_horizontal_wind_shear(left[0], right[0], left[3], left[4], right[3], right[4], environment.config)
        midpoint = ((left[0][0] + right[0][0]) / 2.0, (left[0][1] + right[0][1]) / 2.0)
        horizontal_results.append((index, midpoint, result))

    valid_horizontal = [item for item in horizontal_results if item[2]["status"] == "valid"]

    def maximum(items, key):
        values = [(item, item[2].get(key)) for item in items if item[2].get(key) is not None]
        return max(values, key=lambda pair: pair[1])[0] if values else None

    max_horizontal = maximum(valid_horizontal, "horizontal_shear_s1")
    max_horizontal_direction = maximum(valid_horizontal, "direction_change_deg")
    rank = {SHEAR_MISSING: -1, SHEAR_SAFE: 0, SHEAR_CAUTION: 1, SHEAR_NO_FLY: 2}
    horizontal_active = bool(environment.config.enabled and environment.config.horizontal.enabled)
    horizontal_level = max(
        (item[2]["shear_level"] for item in horizontal_results),
        key=lambda level: rank.get(level, -1),
        default=SHEAR_MISSING,
    ) if horizontal_active else SHEAR_MISSING
    highest = horizontal_level
    blocked = [item for item in horizontal_results if item[2].get("is_flyable") is False]

    def location(item, shear_type):
        if item is None:
            return None
        index, point, result = item
        return {
            "lon": round(point[0], 6),
            "lat": round(point[1], 6),
            "height": getattr(wind_field, "level", None),
            "segment_index": index,
            "shear_type": shear_type,
            "shear_level": result["shear_level"],
        }

    return {
        "enabled": horizontal_active,
        "mode": "horizontal_edges_only",
        "vertical_status": "disabled",
        "vertical_layer_pair_m": None,
        "max_vertical_shear_s1": None,
        "max_vertical_delta_v_10m_ms": None,
        "max_vertical_delta_v_30m_ms": None,
        "max_vertical_direction_change_deg": None,
        "max_horizontal_shear_s1": None if max_horizontal is None else round(max_horizontal[2]["horizontal_shear_s1"], 6),
        "max_horizontal_delta_v_1km_ms": None if not valid_horizontal else round(max(item[2]["delta_v_1km_ms"] for item in valid_horizontal), 3),
        "max_horizontal_direction_change_deg": None if max_horizontal_direction is None else round(max_horizontal_direction[2]["direction_change_deg"], 2),
        "vertical_shear_warning_count": 0,
        "horizontal_shear_warning_count": sum(item[2]["shear_level"] == SHEAR_NO_FLY for item in horizontal_results)
        if horizontal_active else 0,
        "vertical_shear_evaluation_count": 0,
        "horizontal_shear_evaluation_count": len(valid_horizontal) if horizontal_active else 0,
        "blocked_by_wind_shear_count": len(blocked),
        "highest_shear_level": highest,
        "vertical_shear_level": SHEAR_MISSING,
        "horizontal_shear_level": horizontal_level,
        "max_vertical_location": None,
        "max_horizontal_location": location(max_horizontal, "horizontal"),
        "max_shear_location": location(max_horizontal, "horizontal"),
        "note": f"{environment.config.note}；当前仅将相邻水平节点间的风矢量切变作为边约束，暂不考虑垂直风切变",
    }
