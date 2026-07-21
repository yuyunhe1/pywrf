"""QGC WPL 110 mission-file parsing and generation."""

from __future__ import annotations

import math
from typing import Any, Iterable


QGC_WPL_HEADER = "QGC WPL 110"
QGC_WPL_FIELDS = (
    "index",
    "current",
    "frame",
    "command",
    "param1",
    "param2",
    "param3",
    "param4",
    "latitude",
    "longitude",
    "altitude",
    "autocontinue",
)


def _finite_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"航点字段 {field} 不是有效数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"航点字段 {field} 必须是有限数值")
    return number


def _integer(value: Any, field: str) -> int:
    number = _finite_float(value, field)
    if not number.is_integer():
        raise ValueError(f"航点字段 {field} 必须是整数")
    return int(number)


def normalize_qgc_item(item: dict[str, Any], default_index: int = 0) -> dict[str, Any]:
    """Validate one mission item and return canonical QGC field names."""

    normalized = {
        "index": _integer(item.get("index", item.get("seq", default_index)), "index"),
        "current": _integer(item.get("current", 0), "current"),
        "frame": _integer(item.get("frame", 3), "frame"),
        "command": _integer(item.get("command", 16), "command"),
        "param1": _finite_float(item.get("param1", 0.0), "param1"),
        "param2": _finite_float(item.get("param2", 0.0), "param2"),
        "param3": _finite_float(item.get("param3", 0.0), "param3"),
        "param4": _finite_float(item.get("param4", 0.0), "param4"),
        "latitude": _finite_float(
            item.get("latitude", item.get("x_lat", item.get("lat", 0.0))),
            "latitude",
        ),
        "longitude": _finite_float(
            item.get("longitude", item.get("y_long", item.get("lon", 0.0))),
            "longitude",
        ),
        "altitude": _finite_float(
            item.get("altitude", item.get("z_alt", item.get("alt", 0.0))),
            "altitude",
        ),
        "autocontinue": _integer(item.get("autocontinue", 1), "autocontinue"),
    }
    if not -90.0 <= normalized["latitude"] <= 90.0:
        raise ValueError("航点纬度必须位于 -90 到 90 度之间")
    if not -180.0 <= normalized["longitude"] <= 180.0:
        raise ValueError("航点经度必须位于 -180 到 180 度之间")
    if normalized["current"] not in (0, 1):
        raise ValueError("航点 current 只能是 0 或 1")
    if normalized["autocontinue"] not in (0, 1):
        raise ValueError("航点 autocontinue 只能是 0 或 1")
    return normalized


def parse_qgc_waypoints(text: str) -> list[dict[str, Any]]:
    """Parse QGC WPL 110 text into validated mission items."""

    lines = [line.strip() for line in text.replace("\ufeff", "").splitlines() if line.strip()]
    if not lines or lines[0] != QGC_WPL_HEADER:
        raise ValueError("航点文件首行必须是 QGC WPL 110")
    items = []
    for line_number, line in enumerate(lines[1:], start=2):
        columns = line.split()
        if len(columns) != len(QGC_WPL_FIELDS):
            raise ValueError(
                f"航点文件第 {line_number} 行应有 12 列，实际为 {len(columns)} 列"
            )
        raw_item = dict(zip(QGC_WPL_FIELDS, columns))
        try:
            items.append(normalize_qgc_item(raw_item, len(items)))
        except ValueError as exc:
            raise ValueError(f"航点文件第 {line_number} 行无效：{exc}") from exc
    if not items:
        raise ValueError("航点文件没有任务项")
    return items


def serialize_qgc_waypoints(items: Iterable[dict[str, Any]]) -> str:
    """Serialize mission items using tabs and Windows-compatible CRLF lines."""

    normalized = [normalize_qgc_item(item, index) for index, item in enumerate(items)]
    if not normalized:
        raise ValueError("至少需要一个航点任务项")
    lines = [QGC_WPL_HEADER]
    for item in normalized:
        lines.append(
            "\t".join(
                (
                    str(item["index"]),
                    str(item["current"]),
                    str(item["frame"]),
                    str(item["command"]),
                    f'{item["param1"]:.8f}',
                    f'{item["param2"]:.8f}',
                    f'{item["param3"]:.8f}',
                    f'{item["param4"]:.8f}',
                    f'{item["latitude"]:.8f}',
                    f'{item["longitude"]:.8f}',
                    f'{item["altitude"]:.6f}',
                    str(item["autocontinue"]),
                )
            )
        )
    return "\r\n".join(lines) + "\r\n"


def route_points_from_qgc_items(items: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
    """Extract georeferenced mission items for map display and route analysis."""

    points: list[dict[str, float]] = []
    for index, raw_item in enumerate(items):
        item = normalize_qgc_item(raw_item, index)
        latitude = item["latitude"]
        longitude = item["longitude"]
        if math.isclose(latitude, 0.0) and math.isclose(longitude, 0.0):
            continue
        point: dict[str, float] = {
            "lon": longitude,
            "lat": latitude,
        }
        if item["frame"] == 3:
            point.update(
                {
                    "altitude_amsl_m": item["altitude"],
                    "altitude_agl_m": item["altitude"],
                    "terrain_height_m": 0.0,
                }
            )
        else:
            point["altitude_amsl_m"] = item["altitude"]
        if points and math.isclose(points[-1]["lon"], longitude) and math.isclose(points[-1]["lat"], latitude):
            points[-1] = point
        else:
            points.append(point)
    if len(points) < 2:
        raise ValueError("航点文件中至少需要两个带有效经纬度的任务项")
    return points


def _point_values(point: Any, default_agl_m: float) -> tuple[float, float, float, float]:
    if isinstance(point, dict):
        longitude = _finite_float(point.get("lon", point.get("longitude")), "longitude")
        latitude = _finite_float(point.get("lat", point.get("latitude")), "latitude")
        agl = point.get("altitude_agl_m", point.get("agl_m", default_agl_m))
        amsl = point.get("altitude_amsl_m", point.get("ele"))
        terrain = point.get("terrain_height_m", 0.0)
    else:
        longitude = _finite_float(point[0], "longitude")
        latitude = _finite_float(point[1], "latitude")
        amsl = point[2] if len(point) > 2 else None
        agl = point[3] if len(point) > 3 else default_agl_m
        terrain = point[4] if len(point) > 4 else 0.0
    agl_value = default_agl_m if agl is None else _finite_float(agl, "altitude_agl_m")
    if amsl is None:
        amsl_value = agl_value + _finite_float(terrain, "terrain_height_m")
    else:
        amsl_value = _finite_float(amsl, "altitude_amsl_m")
    return longitude, latitude, amsl_value, agl_value


def build_qgc_mission_items(
    points: Iterable[Any],
    default_agl_m: float = 100.0,
) -> list[dict[str, Any]]:
    """Create a fixed-wing Home/Takeoff/Waypoint/Land mission from route points."""

    route = [_point_values(point, default_agl_m) for point in points]
    if len(route) < 2:
        raise ValueError("生成航点文件至少需要两个航线点")
    start = route[0]
    end = route[-1]
    items: list[dict[str, Any]] = [
        {
            "index": 0,
            "current": 1,
            "frame": 0,
            "command": 16,
            "param1": 0.0,
            "param2": 0.0,
            "param3": 0.0,
            "param4": 0.0,
            "latitude": start[1],
            "longitude": start[0],
            "altitude": start[2],
            "autocontinue": 1,
        },
        {
            "index": 1,
            "current": 0,
            "frame": 3,
            "command": 22,
            "param1": 15.0,
            "param2": 0.0,
            "param3": 0.0,
            "param4": 0.0,
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": start[3],
            "autocontinue": 1,
        },
    ]
    for point in route[1:-1]:
        items.append(
            {
                "index": len(items),
                "current": 0,
                "frame": 3,
                "command": 16,
                "param1": 0.0,
                "param2": 0.0,
                "param3": 0.0,
                "param4": 0.0,
                "latitude": point[1],
                "longitude": point[0],
                "altitude": point[3],
                "autocontinue": 1,
            }
        )
    items.append(
        {
            "index": len(items),
            "current": 0,
            "frame": 3,
            "command": 21,
            "param1": 5.0,
            "param2": 0.0,
            "param3": 0.0,
            "param4": 1.0,
            "latitude": end[1],
            "longitude": end[0],
            "altitude": 0.0,
            "autocontinue": 1,
        }
    )
    return [normalize_qgc_item(item, index) for index, item in enumerate(items)]
