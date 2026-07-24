"""FastAPI entrypoint for the GFS wind-risk service."""

import os
import threading
import math

import numpy as np

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse
import json
from datetime import datetime
from pathlib import Path
from .models import ExportedRouteRenameRequest, RouteAnalyzeRequest, RouteDecisionRequest, RoutePlanRequest, RouteRecord
from .qgc_waypoints import (
    QGC_WPL_FIELDS,
    QGC_WPL_HEADER,
    build_qgc_mission_items,
    normalize_qgc_item,
    parse_qgc_waypoints,
    route_points_from_qgc_items,
    serialize_qgc_waypoints,
)

EXPORT_DIR = Path("data/exported_routes")
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


ROUTE_PLAN_MAX_GRID_CELLS = _int_env("ROUTE_PLAN_MAX_GRID_CELLS", 20000)


def _safe_export_name(name: str | None) -> str:
    safe_name = "".join(c for c in (name or "") if c.isalnum() or c in (" ", "-", "_")).strip()
    return safe_name or "未命名航线"


def _unique_export_path(route_name: str, timestamp: str, exclude: Path | None = None) -> Path:
    safe_name = _safe_export_name(route_name)
    file_path = EXPORT_DIR / f"{safe_name}_{timestamp}.json"
    if (
        (not file_path.exists() or (exclude is not None and file_path == exclude))
        and not file_path.with_suffix(".waypoints").exists()
    ):
        return file_path
    index = 1
    while True:
        candidate = EXPORT_DIR / f"{safe_name}_{index}_{timestamp}.json"
        if (
            (not candidate.exists() or (exclude is not None and candidate == exclude))
            and not candidate.with_suffix(".waypoints").exists()
        ):
            return candidate
        index += 1


def _exported_route_path(file_name: str) -> Path:
    requested = Path(file_name)
    if requested.name != file_name or requested.suffix.lower() != ".json":
        raise HTTPException(status_code=400, detail="非法 JSON 文件名")
    return EXPORT_DIR / requested.name


def _exported_waypoint_path(file_name: str) -> Path:
    requested = Path(file_name)
    if requested.name != file_name or requested.suffix.lower() != ".waypoints":
        raise HTTPException(status_code=400, detail="非法航点文件名")
    return EXPORT_DIR / requested.name


def _safe_json_file_name(file_name: str | None) -> str:
    raw_name = (file_name or "").strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="JSON 文件名不能为空")
    requested = Path(raw_name)
    if requested.name != raw_name:
        raise HTTPException(status_code=400, detail="非法 JSON 文件名")
    if requested.suffix and requested.suffix.lower() != ".json":
        raise HTTPException(status_code=400, detail="JSON 文件名必须以 .json 结尾")
    stem = requested.stem if requested.suffix else requested.name
    return f"{_safe_export_name(stem)}.json"


def _unique_export_file_path(file_name: str, exclude: Path | None = None) -> Path:
    target_path = EXPORT_DIR / _safe_json_file_name(file_name)
    excluded_waypoint_path = exclude.with_suffix(".waypoints") if exclude is not None else None
    if (
        (not target_path.exists() or (exclude is not None and target_path == exclude))
        and (
            not target_path.with_suffix(".waypoints").exists()
            or target_path.with_suffix(".waypoints") == excluded_waypoint_path
        )
    ):
        return target_path
    index = 1
    while True:
        candidate = EXPORT_DIR / f"{target_path.stem}_{index}.json"
        if (
            (not candidate.exists() or (exclude is not None and candidate == exclude))
            and (
                not candidate.with_suffix(".waypoints").exists()
                or candidate.with_suffix(".waypoints") == excluded_waypoint_path
            )
        ):
            return candidate
        index += 1


def _exported_route_record(file: Path) -> dict:
    route_id = None
    mission_name = None
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            route_id = payload.get("route_id")
            mission_name = payload.get("mission_name") or payload.get("name") or payload.get("route_name")
    except Exception:
        payload = None
    parts = file.stem.rsplit("_", 1)
    name = mission_name or (parts[0] if len(parts) > 1 else file.stem)
    time_str = parts[1] if len(parts) > 1 else ""
    try:
        dt = datetime.strptime(time_str, "%Y%m%d%H%M%S")
        formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        formatted_time = time_str
    if route_id:
        try:
            route = route_storage.get_route(route_id)
            if route:
                name = route["name"]
        except Exception:
            pass
    return {
        "file_name": file.name,
        "waypoint_file_name": file.with_suffix(".waypoints").name
        if file.with_suffix(".waypoints").exists()
        else None,
        "route_name": name,
        "route_id": route_id,
        "time": formatted_time,
        "timestamp": file.stat().st_mtime,
    }


def _unique_route_id_by_name(route_name: str | None) -> str | None:
    if not route_name:
        return None
    matches = [route for route in route_storage.list_routes() if route.get("name") == route_name]
    return matches[0]["route_id"] if len(matches) == 1 else None


def _export_route_payload_route_id(file_path: Path) -> str | None:
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload.get("route_id") if isinstance(payload, dict) else None


def _update_export_route_name(file_path: Path, route_name: str, route_id: str | None = None) -> None:
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        payload = None
    if isinstance(payload, dict):
        payload["mission_name"] = route_name
        if route_id and not payload.get("route_id"):
            payload["route_id"] = route_id
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _export_files_for_route(route_id: str):
    if not route_id or not EXPORT_DIR.exists():
        return []
    return [file for file in EXPORT_DIR.glob("*.json") if _export_route_payload_route_id(file) == route_id]


def delete_export_files_for_route(route_id: str) -> None:
    for file in _export_files_for_route(route_id):
        for paired_file in (file, file.with_suffix(".waypoints")):
            try:
                paired_file.unlink()
            except FileNotFoundError:
                pass


def export_route_to_json(record: RouteRecord, route_id: str | None = None):
    def _parse_level_agl(level: str | None) -> float | None:
        if not level:
            return None
        text = level.lower().replace("agl", "").replace(" ", "")
        number = ""
        for char in text:
            if char.isdigit() or char == ".":
                number += char
            elif number:
                break
        return float(number) if number else None

    def _bearing_deg(start: tuple[float, float], end: tuple[float, float]) -> float:
        lon1, lat1, lon2, lat2 = map(math.radians, (start[0], start[1], end[0], end[1]))
        dlon = lon2 - lon1
        y = math.sin(dlon) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    default_agl = _parse_level_agl(record.level)
    waypoints = []
    for idx, point in enumerate(record.points):
        lon = float(point[0])
        lat = float(point[1])
        altitude_amsl = float(point[2]) if len(point) > 2 and np.isfinite(point[2]) else None
        altitude_agl = float(point[3]) if len(point) > 3 and np.isfinite(point[3]) else default_agl
        terrain_height = float(point[4]) if len(point) > 4 and np.isfinite(point[4]) else None
        if altitude_amsl is None and altitude_agl is not None and terrain_height is not None:
            altitude_amsl = altitude_agl + terrain_height
        if terrain_height is None and altitude_amsl is not None and altitude_agl is not None:
            terrain_height = altitude_amsl - altitude_agl
        if altitude_amsl is None:
            altitude_amsl = 0.0
        heading = None
        if idx < len(record.points) - 1:
            next_point = record.points[idx + 1]
            heading = round(_bearing_deg((lon, lat), (float(next_point[0]), float(next_point[1]))), 1)
        elif waypoints:
            heading = waypoints[-1].get("heading_deg")
        waypoints.append({
            "point_index": idx + 1,
            "seq": idx + 1,
            "lon": lon,
            "lat": lat,
            "ele": round(float(altitude_amsl), 2),
            "altitude_mode": "AGL",
            "altitude_agl_m": None if altitude_agl is None else round(float(altitude_agl), 2),
            "terrain_height_m": None if terrain_height is None else round(float(terrain_height), 2),
            "altitude_amsl_m": round(float(altitude_amsl), 2),
            "heading_deg": heading,
            "speed_mps": 10.0,
            "action": "waypoint",
            "hold_time_s": 0,
        })
    
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    file_path = _unique_export_path(record.name, timestamp)
    waypoint_file_path = file_path.with_suffix(".waypoints")
    if record.mission_items:
        mission_items = [
            normalize_qgc_item(item, index)
            for index, item in enumerate(record.mission_items)
        ]
    else:
        mission_items = build_qgc_mission_items(
            record.points,
            default_agl_m=default_agl if default_agl is not None else 100.0,
        )
    payload = {
        "route_id": route_id,
        "mission_name": record.name,
        "coordinate_system": "WGS84",
        "altitude_mode": "AGL",
        "altitude_reference": "terrain_following",
        "default_cruise_speed_mps": 10.0,
        "level": record.level,
        "cycle": record.cycle,
        "forecast_hour": record.forecast_hour,
        "waypoint_schema": {
            "ele": "legacy altitude field, meters AMSL",
            "altitude_agl_m": "height above local terrain",
            "terrain_height_m": "surface elevation/HGT, meters AMSL",
            "altitude_amsl_m": "terrain_height_m + altitude_agl_m",
        },
        "waypoints": waypoints,
        "qgc_wpl": {
            "format": QGC_WPL_HEADER,
            "fields": list(QGC_WPL_FIELDS),
            "waypoint_file_name": waypoint_file_path.name,
        },
        "mission_items": mission_items,
    }
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(waypoint_file_path, "w", encoding="utf-8", newline="") as f:
        f.write(serialize_qgc_waypoints(mission_items))
    return _exported_route_record(file_path)

from .data_provider import (
    availability,
    data_mode,
    diagnostics,
    get_grid,
    get_grid_by_valid_time,
    gfs_download_status,
    maybe_start_gfs_download,
    refresh,
    start_gfs_download,
)
from .decision_service import decide_navigation
from .grid_planner_lpa_star import LPAStarPlanner
from .grid_planner_wa_lpa_star import WALPAStarPlanner
from .multi_altitude_routing import level_height_m, plan_multi_altitude_route
from .route_options import (
    PLANNING_STRATEGY_WIND_AVOIDANCE,
    build_strategy_cost_config,
    describe_aircraft_model,
    describe_strategy,
    normalize_aircraft_model,
    normalize_planning_strategy,
    route_search_padding_deg,
)
from .routing import plan_route
from . import route_storage, wrf_cache_provider
from .route_service import (
    analyze_route,
    anchor_route_endpoints,
    haversine_km,
    include_wind_shear_in_high_risk_ratio,
    risk_name,
    sample_route,
)
from .wind_provider import WindGrid, parse_bbox, point_value
from .wind_shear import (
    SHEAR_NO_FLY,
    WindShearBlockedError,
    WindShearEnvironment,
    analyze_route_wind_shear,
    ensure_wind_shear_config,
)

app = FastAPI(title="UAV Low-altitude Wind Risk API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _sync_wrf_cache_on_startup() -> None:
    try:
        status = wrf_cache_provider.sync_latest_cycle(force_index=True)
        print(f"[WRF_CACHE] startup sync: {status}")
    except Exception as exc:
        print(f"[WRF_CACHE] startup sync failed: {exc}")


@app.on_event("startup")
def check_latest_gfs_on_startup():
    """Check and download the latest realtime GFS cycle when the API starts."""
    if data_mode() == "real":
        start_gfs_download("backend startup latest GFS check")
    if _env_enabled("WRF_CACHE_SYNC_ON_STARTUP", True) and wrf_cache_provider.remote_configured():
        threading.Thread(target=_sync_wrf_cache_on_startup, daemon=True).start()


def load_grid(
    cycle: str | None,
    forecast_hour: int | None,
    level: str,
    bbox: str | None = None,
    valid_time: str | None = None,
    source: str | None = None,
):
    """Validate common query parameters and load a cached regular grid."""
    try:
        parsed_bbox = parse_bbox(bbox)
        if valid_time:
            return get_grid_by_valid_time(valid_time, level, parsed_bbox, source)
        if cycle is None or forecast_hour is None:
            raise ValueError("未提供 valid_time 时，必须提供 cycle 和 forecast_hour")
        return get_grid(cycle, forecast_hour, level, parsed_bbox, source)
    except ValueError as exc:
        download = maybe_start_gfs_download(source, str(exc))
        if download and download.get("running"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": f"{exc}. 已自动启动实时 GFS 下载，请稍后刷新。",
                    "download": download,
                },
            ) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def metadata(grid) -> dict:
    """Build common wind-field metadata."""
    return {
        "source": grid.source,
        "cycle": grid.cycle,
        "cycle_bj": getattr(grid, "cycle_bj", None),
        "forecast_hour": grid.forecast_hour,
        "valid_time": grid.valid_time,
        "valid_time_bj": getattr(grid, "valid_time_bj", None),
        "level": grid.level,
        "unit": "m/s",
        "bbox": [float(grid.lons[0]), float(grid.lats[-1]), float(grid.lons[-1]), float(grid.lats[0])],
        "grid": {
            "nx": len(grid.lons),
            "ny": len(grid.lats),
            "dx": round(float(grid.lons[1] - grid.lons[0]), 8),
            "dy": round(float(grid.lats[0] - grid.lats[1]), 8),
        },
    }


def route_planning_bbox(start: tuple[float, float], end: tuple[float, float], strategy: str | None = None) -> str:
    """Build a generously padded lon/lat bbox for grid route search."""

    padding = route_search_padding_deg(start, end, strategy)
    min_lon = max(-180.0, min(start[0], end[0]) - padding)
    max_lon = min(180.0, max(start[0], end[0]) + padding)
    min_lat = max(-90.0, min(start[1], end[1]) - padding)
    max_lat = min(90.0, max(start[1], end[1]) + padding)
    return f"{min_lon},{min_lat},{max_lon},{max_lat}"


def candidate_route_levels(selected_level: str) -> list[str]:
    """Return AGL layers to search for multi-altitude route planning."""

    configured = os.getenv("ROUTE_PLAN_AGL_LEVELS")
    if configured:
        values = []
        for item in configured.split(","):
            item = item.strip()
            if not item:
                continue
            height = level_height_m(item if "m" in item.lower() else f"{item}m AGL")
            if height is not None:
                values.append(float(height))
        levels = values
    else:
        selected_height = level_height_m(selected_level) or 100.0
        available = [80.0, 100.0, 200.0, 300.0, 500.0]
        nearby = [height for height in available if abs(height - selected_height) <= 250.0]
        levels = sorted(set([selected_height, *nearby]))
    return [f"{int(round(value))}m AGL" for value in sorted(set(levels)) if value > 0]


def build_route_wind_shear_environment(
    request: RouteAnalyzeRequest,
    route_bbox: str,
    base_grid: WindGrid,
    stride: int = 1,
) -> WindShearEnvironment:
    """Build horizontal-edge shear settings without loading other heights."""

    del route_bbox, base_grid, stride
    config = ensure_wind_shear_config(request.wind_shear)
    return WindShearEnvironment(config=config)


def horizontal_wind_shear_response(shear_analysis: dict) -> dict:
    """Expose the final-route horizontal-shear profile at the API level."""

    return {
        "max_horizontal_wind_shear": shear_analysis.get("max_horizontal_wind_shear"),
        "max_horizontal_wind_shear_unit": shear_analysis.get(
            "max_horizontal_wind_shear_unit", "m/s"
        ),
        "max_horizontal_wind_shear_segment": shear_analysis.get(
            "max_horizontal_wind_shear_segment"
        ),
        "horizontal_wind_shear_profile": shear_analysis.get(
            "horizontal_wind_shear_profile", []
        ),
    }


def apply_navigation_decision(analysis: dict, thresholds) -> dict:
    """Attach one binary navigation decision and keep risk details separate."""

    shear = analysis.get("wind_shear") or {}
    horizontal_shear_risk = bool(
        analysis.get("wind_shear_fallback")
        or analysis.get("wind_shear_failure")
        or shear.get("highest_shear_level") == SHEAR_NO_FLY
        or int(shear.get("horizontal_shear_warning_count", 0) or 0) > 0
    )
    endpoint_block = analysis.get("endpoint_hard_block")
    max_wind_speed = analysis.get("max_wind_speed")
    hard_wind_risk = bool(
        max_wind_speed is not None
        and math.isfinite(float(max_wind_speed))
        and float(max_wind_speed) >= float(thresholds.danger)
    )
    prohibited = bool(endpoint_block or horizontal_shear_risk or hard_wind_risk)

    if endpoint_block:
        reason = endpoint_block["message"]
    elif horizontal_shear_risk:
        reason = "存在达到硬约束阈值的水平风切变，禁止通航。"
    elif hard_wind_risk:
        reason = f"航线最大风速达到或超过 {float(thresholds.danger):g} m/s，禁止通航。"
    else:
        reason = "航线未触发风速或水平风切变硬约束，允许通航。"

    analysis.update(
        {
            "navigation_allowed": not prohibited,
            "navigation_decision": "禁止通航" if prohibited else "允许通航",
            "navigation_decision_reason": reason,
            "horizontal_wind_shear_risk": horizontal_shear_risk,
        }
    )
    return analysis


def endpoint_hard_wind_block(
    grid: WindGrid,
    start: tuple[float, float],
    end: tuple[float, float],
    hard_limit_ms: float,
) -> dict | None:
    """Return endpoint hard-limit details before any graph search starts."""

    endpoints = []
    for name, point in (("起点", start), ("终点", end)):
        wind = point_value(grid, point[0], point[1])
        speed = float(wind["wind_speed"])
        endpoints.append(
            {
                "name": name,
                "lon": float(point[0]),
                "lat": float(point[1]),
                "wind_speed": round(speed, 3) if math.isfinite(speed) else None,
                "blocked": not math.isfinite(speed) or speed >= hard_limit_ms,
            }
        )
    blocked = [item for item in endpoints if item["blocked"]]
    if not blocked:
        return None
    blocked_names = "、".join(item["name"] for item in blocked)
    return {
        "active": True,
        "reason": "endpoint_wind_speed",
        "hard_limit_ms": float(hard_limit_ms),
        "endpoints": endpoints,
        "message": f"{blocked_names}风速达到或超过 {float(hard_limit_ms):g} m/s 硬上限，禁止通航，已跳过航线图搜索。",
    }


def endpoint_blocked_route_response(
    request: RoutePlanRequest,
    raw_grid: WindGrid,
    route_bbox: str,
    endpoint_block: dict,
    shear_environment: WindShearEnvironment,
    aircraft_model: str,
    planning_strategy: str,
    planner_type: str,
    cost_config: dict,
) -> dict:
    """Build a complete fast-fail response without inventing a route."""

    endpoint_speeds = [
        float(item["wind_speed"])
        for item in endpoint_block["endpoints"]
        if item["wind_speed"] is not None
    ]
    max_wind_speed = max(endpoint_speeds) if endpoint_speeds else None
    mean_wind_speed = float(np.mean(endpoint_speeds)) if endpoint_speeds else None
    high_risk_count = sum(
        speed > float(request.thresholds.warning) for speed in endpoint_speeds
    )
    shear_analysis = analyze_route_wind_shear([], raw_grid, shear_environment)
    shear_analysis.update(
        {
            "max_horizontal_wind_shear": None,
            "max_horizontal_wind_shear_segment": None,
            "horizontal_wind_shear_profile": [],
            "final_route_available": False,
        }
    )
    analysis = {
        "max_wind_speed": None if max_wind_speed is None else round(max_wind_speed, 2),
        "mean_wind_speed": None if mean_wind_speed is None else round(mean_wind_speed, 2),
        "danger_ratio": round(high_risk_count / len(endpoint_speeds), 3) if endpoint_speeds else 0.0,
        "wind_only_danger_ratio": round(high_risk_count / len(endpoint_speeds), 3) if endpoint_speeds else 0.0,
        "wind_shear_risk_ratio": 0.0,
        "high_risk_evaluation_count": high_risk_count,
        "risk_evaluation_count": len(endpoint_speeds),
        "risk_level": "-" if max_wind_speed is None else risk_name(max_wind_speed, request.thresholds),
        "total_distance_km": 0.0,
        "max_continuous_danger_km": 0.0,
        "tailwind_ratio": 0.0,
        "samples": [],
        "endpoint_hard_block": endpoint_block,
        "wind_shear": shear_analysis,
        **horizontal_wind_shear_response(shear_analysis),
    }
    apply_navigation_decision(analysis, request.thresholds)
    return {
        "points": [],
        "segments": [],
        "cost": None,
        "distance_km": 0.0,
        "level": raw_grid.level,
        "planner_type": planner_type,
        "aircraft_model": aircraft_model,
        "aircraft_model_label": describe_aircraft_model(aircraft_model),
        "planning_strategy": planning_strategy,
        "planning_strategy_label": describe_strategy(planning_strategy),
        "cost_config": cost_config,
        "search_bbox": [float(value) for value in route_bbox.split(",")],
        "search_grid_stride": 1,
        "multi_altitude": False,
        "planning_skipped": True,
        "planning_skip_reason": "endpoint_wind_speed",
        "endpoint_hard_block": endpoint_block,
        **horizontal_wind_shear_response(shear_analysis),
        "wind_shear": shear_analysis,
        "analysis": analysis,
        "metadata": metadata(raw_grid),
    }


def load_candidate_route_grids(request: RoutePlanRequest, route_bbox: str) -> list[WindGrid]:
    """Load all usable candidate AGL grids for multi-altitude planning."""

    grids: list[WindGrid] = []
    for level in candidate_route_levels(request.level):
        try:
            grid = load_grid(request.cycle, request.forecast_hour, level, route_bbox, request.valid_time, request.source)
        except HTTPException:
            continue
        if getattr(grid, "terrain", None) is None:
            grid = WindGrid(
                lons=grid.lons,
                lats=grid.lats,
                u=grid.u,
                v=grid.v,
                cycle=grid.cycle,
                forecast_hour=grid.forecast_hour,
                level=grid.level,
                valid_time=grid.valid_time,
                source=grid.source,
                cycle_bj=getattr(grid, "cycle_bj", None),
                valid_time_bj=getattr(grid, "valid_time_bj", None),
                terrain=np.zeros_like(grid.u, dtype=float),
            )
        grids.append(grid)
    unique: dict[str, WindGrid] = {}
    for grid in grids:
        unique[grid.level] = grid
    return list(unique.values())


def thin_route_planning_grid(grid: WindGrid) -> tuple[WindGrid, int]:
    """Downsample only the planner grid when a long route loads too many cells.

    The original grid is still used for route risk sampling after planning.
    This keeps long-distance planning responsive while preserving analysis
    against the highest available wind-field resolution.
    """

    nx = len(grid.lons)
    ny = len(grid.lats)
    cell_count = nx * ny
    if ROUTE_PLAN_MAX_GRID_CELLS <= 0 or cell_count <= ROUTE_PLAN_MAX_GRID_CELLS:
        return grid, 1
    stride = max(2, int(np.ceil((cell_count / ROUTE_PLAN_MAX_GRID_CELLS) ** 0.5)))
    lons = grid.lons[::stride]
    lats = grid.lats[::stride]
    if len(lons) < 2 or len(lats) < 2:
        return grid, 1
    return (
        WindGrid(
            lons=lons,
            lats=lats,
            u=grid.u[::stride, ::stride],
            v=grid.v[::stride, ::stride],
            cycle=grid.cycle,
            forecast_hour=grid.forecast_hour,
            level=grid.level,
            valid_time=grid.valid_time,
            source=f"{grid.source} (route-planning stride {stride})",
            cycle_bj=getattr(grid, "cycle_bj", None),
            valid_time_bj=getattr(grid, "valid_time_bj", None),
            terrain=None if getattr(grid, "terrain", None) is None else grid.terrain[::stride, ::stride],
        ),
        stride,
    )


def enrich_route_points_with_altitude(points: list, grid: WindGrid) -> list:
    """Attach [AMSL, AGL, terrain] to 2-D route points when possible."""

    if not points:
        return points
    if len(points[0]) >= 5:
        return points
    agl = level_height_m(grid.level)
    if agl is None:
        return points
    terrain = getattr(grid, "terrain", None)
    if terrain is None:
        terrain = np.zeros_like(grid.u, dtype=float)
    enriched = []
    for point in points:
        lon, lat = float(point[0]), float(point[1])
        row = int(np.abs(grid.lats - lat).argmin())
        col = int(np.abs(grid.lons - lon).argmin())
        terrain_height = float(np.asarray(terrain)[row, col])
        if not np.isfinite(terrain_height):
            terrain_height = 0.0
        enriched.append([lon, lat, round(terrain_height + agl, 2), round(float(agl), 2), round(terrain_height, 2)])
    return enriched


def plan_single_altitude_route(
    grid: WindGrid,
    start: tuple[float, float],
    end: tuple[float, float],
    thresholds,
    cost_config,
    planner_type: str,
    wind_shear_environment: WindShearEnvironment | None = None,
) -> dict:
    """Run the selected planner for one fixed altitude layer."""

    if planner_type in {"astar", "a_star"}:
        result = plan_route(
            grid,
            start,
            end,
            thresholds,
            cost_config=cost_config,
            wind_shear_environment=wind_shear_environment,
        )
        result["planner_type"] = planner_type
        return result
    if planner_type in {"lpa", "lpa_star", "wa_lpa", "wa_lpa_star", "walpa", "walpa_star"}:
        planner_class = LPAStarPlanner if planner_type in {"lpa", "lpa_star"} else WALPAStarPlanner
        planner = planner_class(
            grid,
            {"lon": start[0], "lat": start[1]},
            {"lon": end[0], "lat": end[1]},
            cost_config=cost_config,
            thresholds=thresholds,
            wind_shear_environment=wind_shear_environment,
        )
        plan = planner.plan()
        points = anchor_route_endpoints(list(plan.points), start, end)
        if not points:
            if plan.wind_shear_blocked_count:
                raise WindShearBlockedError(plan.wind_shear_blocked_count)
            raise ValueError("未找到可行路径")
        distance_km = sum(haversine_km(point_from, point_to) for point_from, point_to in zip(points, points[1:]))
        shear_analysis_points = anchor_route_endpoints(
            [planner.cell_point(node) for node in planner.node_path()],
            start,
            end,
        )
        return {
            "points": points,
            "segments": [],
            "cost": plan.total_cost,
            "distance_km": round(distance_km, 3),
            "level": grid.level,
            "planner_type": planner_type,
            "planning_time_ms": plan.planning_time_ms,
            "expanded_nodes": plan.expanded_nodes,
            "search_wind_shear_blocked_edges": plan.wind_shear_blocked_count,
            "shear_analysis_points": shear_analysis_points,
        }
    raise ValueError("planner_type must be astar, lpa_star, or wa_lpa_star")


def _route_choice_score(points: list, grid: WindGrid, thresholds, sample_interval_km: float) -> tuple:
    samples = sample_route(points, grid, thresholds, sample_interval_km)
    summary = analyze_route(samples, thresholds)
    return (
        float(summary.get("danger_ratio", 1.0)),
        float(summary.get("max_wind_speed", float("inf"))),
        float(summary.get("mean_wind_speed", float("inf"))),
        float(summary.get("total_distance_km", float("inf"))),
    )


def compare_forward_reverse_routes(
    grid: WindGrid,
    start: tuple[float, float],
    end: tuple[float, float],
    thresholds,
    cost_config,
    planner_type: str,
    sample_interval_km: float,
    enable_reverse: bool = True,
    wind_shear_environment: WindShearEnvironment | None = None,
) -> dict:
    """Plan start->end and end->start, then score both as start->end geometry."""

    forward = plan_single_altitude_route(
        grid,
        start,
        end,
        thresholds,
        cost_config,
        planner_type,
        wind_shear_environment,
    )
    if not enable_reverse or not _env_enabled("ROUTE_PLAN_REVERSE_COMPARE", True):
        forward["search_direction"] = "forward"
        return forward
    try:
        backward = plan_single_altitude_route(
            grid,
            end,
            start,
            thresholds,
            cost_config,
            planner_type,
            wind_shear_environment,
        )
        backward_points = anchor_route_endpoints(list(reversed(backward["points"])), start, end)
        forward_score = _route_choice_score(forward["points"], grid, thresholds, sample_interval_km)
        backward_score = _route_choice_score(backward_points, grid, thresholds, sample_interval_km)
        if backward_score < forward_score:
            backward["points"] = backward_points
            backward["shear_analysis_points"] = anchor_route_endpoints(
                list(reversed(backward.get("shear_analysis_points", backward_points))),
                start,
                end,
            )
            backward["search_direction"] = "reverse_geometry_selected"
            backward["forward_score"] = forward_score
            backward["selected_score"] = backward_score
            return backward
        forward["search_direction"] = "forward"
        forward["forward_score"] = forward_score
        forward["reverse_score"] = backward_score
        return forward
    except ValueError as exc:
        forward["search_direction"] = "forward"
        forward["reverse_compare_error"] = str(exc)
        return forward


@app.get("/", include_in_schema=False)
def frontend():
    """Redirect the backend root URL to the local Vue development UI."""
    return RedirectResponse("http://127.0.0.1:5173")


@app.get("/api/health")
def health(source: str | None = None):
    """Return service health."""
    try:
        return {"status": "ok", **diagnostics(source)}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/times")
def times(
    refresh_files: bool = Query(default=False, alias="refresh"),
    auto_download: bool = True,
    source: str | None = None,
):
    """Return cycles, forecast hours, and AGL levels discovered from existing GFS files."""
    if refresh_files:
        refresh(source)
    return availability(source, auto_download=auto_download)


@app.post("/api/gfs/download")
def trigger_gfs_download(force: bool = False):
    """Start the realtime GFS downloader used by the backend."""
    status = start_gfs_download("manual API request", force=force)
    if not status.get("enabled") and not force:
        raise HTTPException(status_code=400, detail=status)
    return status


@app.get("/api/gfs/download-status")
def gfs_download():
    """Return the current or most recent realtime GFS download status."""
    return gfs_download_status()


@app.get("/api/wind")
def wind(
    cycle: str | None = None,
    forecast_hour: int | None = Query(default=None, ge=0),
    level: str = "100m AGL",
    bbox: str | None = None,
    valid_time: str | None = None,
    source: str | None = None,
):
    """Return regular-grid U/V arrays in leaflet-velocity JSON format."""
    grid = load_grid(cycle, forecast_hour, level, bbox, valid_time, source)
    dx = float(grid.lons[1] - grid.lons[0])
    dy = float(grid.lats[0] - grid.lats[1])
    common = {
        "parameterUnit": "m.s-1",
        "parameterCategory": 2,
        "nx": len(grid.lons),
        "ny": len(grid.lats),
        "lo1": float(grid.lons[0]),
        "la1": float(grid.lats[0]),
        "lo2": float(grid.lons[-1]),
        "la2": float(grid.lats[-1]),
        "dx": dx,
        "dy": dy,
        "refTime": grid.valid_time,
    }
    velocity = [
        {"header": {**common, "parameterNumber": 2, "parameterNumberName": "eastward_wind"}, "data": grid.u.ravel().round(3).tolist()},
        {"header": {**common, "parameterNumber": 3, "parameterNumberName": "northward_wind"}, "data": grid.v.ravel().round(3).tolist()},
    ]
    return {"metadata": metadata(grid), "velocity": velocity}


@app.get("/api/heatmap")
def heatmap(
    cycle: str | None = None,
    forecast_hour: int | None = Query(default=None, ge=0),
    level: str = "100m AGL",
    bbox: str | None = None,
    valid_time: str | None = None,
    source: str | None = None,
):
    """Return a regular wind-speed matrix for a Leaflet canvas raster layer."""
    grid = load_grid(cycle, forecast_hour, level, bbox, valid_time, source)
    speed = (grid.u**2 + grid.v**2) ** 0.5
    finite_speed = speed[np.isfinite(speed)]
    return {
        "metadata": metadata(grid),
        "wind_speed": {
            "header": {
                "nx": len(grid.lons),
                "ny": len(grid.lats),
                "lo1": float(grid.lons[0]),
                "la1": float(grid.lats[0]),
                "lo2": float(grid.lons[-1]),
                "la2": float(grid.lats[-1]),
                "dx": float(grid.lons[1] - grid.lons[0]),
                "dy": float(grid.lats[0] - grid.lats[1]),
                "unit": "m/s",
            },
            "data": speed.ravel().round(3).tolist(),
            "min": round(float(finite_speed.min()), 3),
            "max": round(float(finite_speed.max()), 3),
        },
    }


@app.get("/api/point")
def point(
    lon: float,
    lat: float,
    cycle: str | None = None,
    forecast_hour: int | None = Query(default=None, ge=0),
    level: str = "100m AGL",
    bbox: str | None = None,
    valid_time: str | None = None,
    source: str | None = None,
):
    """Query the nearest wind grid point. Inputs use longitude then latitude."""
    grid_bbox = bbox or f"{lon - 0.5},{lat - 0.5},{lon + 0.5},{lat + 0.5}"
    grid = load_grid(cycle, forecast_hour, level, grid_bbox, valid_time, source)
    domain = metadata(grid)["bbox"]
    if not (domain[0] <= lon <= domain[2] and domain[1] <= lat <= domain[3]):
        raise HTTPException(status_code=400, detail="请求的坐标点超出了可用风场数据的经纬度范围")
    return {**point_value(grid, lon, lat), "level": grid.level, "valid_time": grid.valid_time, "unit": "m/s"}


@app.post("/api/route/analyze")
def route_analyze(request: RouteAnalyzeRequest):
    """Sample a [lon, lat] route and return wind-risk statistics."""
    lons = [point[0] for point in request.points]
    lats = [point[1] for point in request.points]
    route_bbox = f"{min(lons) - 0.5},{min(lats) - 0.5},{max(lons) + 0.5},{max(lats) + 0.5}"
    grid = load_grid(request.cycle, request.forecast_hour, request.level, route_bbox, request.valid_time, request.source)
    samples = sample_route(request.points, grid, request.thresholds, request.sample_interval_km)
    shear_environment = build_route_wind_shear_environment(request, route_bbox, grid)
    shear_analysis = analyze_route_wind_shear(request.points, grid, shear_environment)
    route_risk = include_wind_shear_in_high_risk_ratio(
        analyze_route(samples, request.thresholds),
        samples,
        shear_analysis,
        request.thresholds,
    )
    response = {
        **route_risk,
        **horizontal_wind_shear_response(shear_analysis),
        "wind_shear": shear_analysis,
        "metadata": metadata(grid),
    }
    return apply_navigation_decision(response, request.thresholds)


@app.post("/api/route/plan")
def route_plan(request: RoutePlanRequest):
    """Plan a single-altitude route between two map points."""
    wind_shear_fallback = None
    try:
        aircraft_model = normalize_aircraft_model(request.aircraft_model)
        planning_strategy = normalize_planning_strategy(request.planning_strategy)
        cost_config = build_strategy_cost_config(planning_strategy)
        route_bbox = route_planning_bbox(request.start, request.end, planning_strategy)
        raw_grid = load_grid(request.cycle, request.forecast_hour, request.level, route_bbox, request.valid_time, request.source)
        raw_shear_environment = build_route_wind_shear_environment(request, route_bbox, raw_grid)
        planner_type = request.planner_type.lower().replace("-", "_")
        endpoint_block = endpoint_hard_wind_block(
            raw_grid,
            request.start,
            request.end,
            float(request.thresholds.danger),
        )
        if endpoint_block is not None:
            return endpoint_blocked_route_response(
                request,
                raw_grid,
                route_bbox,
                endpoint_block,
                raw_shear_environment,
                aircraft_model,
                planning_strategy,
                planner_type,
                cost_config,
            )
        grid, route_grid_stride = thin_route_planning_grid(raw_grid)
        planner_shear_environment = raw_shear_environment
        candidate_grids = load_candidate_route_grids(request, route_bbox) if _env_enabled("ROUTE_PLAN_MULTI_ALTITUDE", False) else []
        result = None
        multi_altitude_error = None
        if len(candidate_grids) >= 2:
            thin_grids = []
            route_grid_stride = 1
            for candidate_grid in candidate_grids:
                thin_grid, stride = thin_route_planning_grid(candidate_grid)
                thin_grids.append(thin_grid)
                route_grid_stride = max(route_grid_stride, stride)
            try:
                result = plan_multi_altitude_route(
                    thin_grids,
                    request.start,
                    request.end,
                    request.thresholds,
                    cost_config=cost_config,
                    wind_shear_config=request.wind_shear,
                )
                result["planner_type"] = f"{planner_type}_multi_altitude"
                result["requested_planner_type"] = planner_type
            except ValueError as exc:
                multi_altitude_error = str(exc)

        if result is None:
            result = compare_forward_reverse_routes(
                grid,
                request.start,
                request.end,
                request.thresholds,
                cost_config,
                planner_type,
                request.sample_interval_km,
                enable_reverse=planning_strategy == PLANNING_STRATEGY_WIND_AVOIDANCE,
                wind_shear_environment=planner_shear_environment,
            )
        result["aircraft_model"] = aircraft_model
        result["aircraft_model_label"] = describe_aircraft_model(aircraft_model)
        result["planning_strategy"] = planning_strategy
        result["planning_strategy_label"] = describe_strategy(planning_strategy)
        result["cost_config"] = cost_config
        result["search_bbox"] = [float(value) for value in route_bbox.split(",")]
        result["search_grid_stride"] = route_grid_stride
        result["multi_altitude"] = str(result.get("planner_type", "")).endswith("_multi_altitude")
        if multi_altitude_error:
            result["multi_altitude_error"] = multi_altitude_error
        result["points"] = enrich_route_points_with_altitude(result["points"], raw_grid)
    except WindShearBlockedError as exc:
        # Preserve a visible reference route for analysis: retry with only the
        # horizontal-shear edge constraint disabled while retaining wind-speed and all
        # other existing route constraints.
        try:
            fallback_config_mapping = request.wind_shear.model_dump()
            fallback_config_mapping["enabled"] = False
            fallback_shear_environment = WindShearEnvironment(
                config=ensure_wind_shear_config(fallback_config_mapping),
            )
            result = compare_forward_reverse_routes(
                grid,
                request.start,
                request.end,
                request.thresholds,
                cost_config,
                planner_type,
                request.sample_interval_km,
                enable_reverse=planning_strategy == PLANNING_STRATEGY_WIND_AVOIDANCE,
                wind_shear_environment=fallback_shear_environment,
            )
            wind_shear_fallback = {
                "active": True,
                "reason": "wind_shear_blocked",
                "message": "水平风切变边约束已完全阻断起终点；地图显示的是忽略水平风切变、仍保留风速等节点约束的参考航线。",
                "blocked_by_wind_shear_count": exc.blocked_count,
            }
            result["wind_shear_fallback"] = wind_shear_fallback
            result["aircraft_model"] = aircraft_model
            result["aircraft_model_label"] = describe_aircraft_model(aircraft_model)
            result["planning_strategy"] = planning_strategy
            result["planning_strategy_label"] = describe_strategy(planning_strategy)
            result["cost_config"] = cost_config
            result["search_bbox"] = [float(value) for value in route_bbox.split(",")]
            result["search_grid_stride"] = route_grid_stride
            result["multi_altitude"] = False
            result["points"] = enrich_route_points_with_altitude(result["points"], raw_grid)
        except ValueError as fallback_exc:
            raise HTTPException(status_code=400, detail=str(fallback_exc)) from fallback_exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "analysis_samples" in result:
        samples = result.pop("analysis_samples")
    else:
        samples = sample_route(result["points"], raw_grid, request.thresholds, request.sample_interval_km)
    route_analysis = analyze_route(samples, request.thresholds)
    shear_analysis_points = result.pop("shear_analysis_points", result["points"])
    shear_analysis = analyze_route_wind_shear(shear_analysis_points, raw_grid, raw_shear_environment)
    if wind_shear_fallback is not None:
        # The returned geometry is only a reference route after the constrained
        # search failed, so it must not be presented as a final-route profile.
        shear_analysis.update(
            {
                "max_horizontal_wind_shear": None,
                "max_horizontal_wind_shear_segment": None,
                "horizontal_wind_shear_profile": [],
                "final_route_available": False,
            }
        )
    else:
        shear_analysis["final_route_available"] = True
    route_analysis = include_wind_shear_in_high_risk_ratio(
        route_analysis,
        samples,
        shear_analysis,
        request.thresholds,
    )
    route_analysis["wind_shear"] = shear_analysis
    route_analysis.update(horizontal_wind_shear_response(shear_analysis))
    if wind_shear_fallback is not None:
        route_analysis["wind_shear_fallback"] = wind_shear_fallback
    apply_navigation_decision(route_analysis, request.thresholds)
    return {
        **result,
        **horizontal_wind_shear_response(shear_analysis),
        "wind_shear": shear_analysis,
        "analysis": route_analysis,
        "metadata": metadata(raw_grid),
    }


@app.post("/api/route/decision")
def route_decision(request: RouteDecisionRequest):
    """Evaluate current/future forecast times and return a navigation decision."""
    lons = [request.start[0], request.end[0]]
    lats = [request.start[1], request.end[1]]
    route_bbox = f"{min(lons) - 0.5},{min(lats) - 0.5},{max(lons) + 0.5},{max(lats) + 0.5}"
    try:
        valid_times = list(dict.fromkeys(request.candidate_valid_times))
        candidates = []
        if valid_times:
            for valid_time in valid_times:
                grid = load_grid(None, None, request.level, route_bbox, valid_time, request.source)
                candidates.append({"forecast_time": valid_time, "wind_field": grid})
        else:
            if request.cycle is None or request.forecast_hour is None:
                if request.valid_time:
                    grid = load_grid(None, None, request.level, route_bbox, request.valid_time, request.source)
                    candidates.append({"forecast_time": request.valid_time, "wind_field": grid})
                else:
                    raise ValueError("必须提供 candidate_valid_times，或提供 cycle/forecast_hour，或提供 valid_time")
            else:
                for offset in request.candidate_offsets_hours:
                    forecast_hour = int(request.forecast_hour) + int(offset)
                    grid = load_grid(request.cycle, forecast_hour, request.level, route_bbox, None, request.source)
                    candidates.append({"forecast_time": grid.valid_time, "wind_field": grid})

        return decide_navigation(
            request.start,
            request.end,
            candidates,
            max_wind_speed_threshold=request.max_wind_speed_threshold,
            max_rain_threshold=request.max_rain_threshold,
            min_agl_height=request.min_agl_height,
            planner_type=request.planner_type,
            max_cumulative_cost=request.max_cumulative_cost,
            planner_hard_max_wind_speed=7.9,
            thresholds=request.thresholds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/routes", status_code=201)
def create_route(record: RouteRecord):
    result = route_storage.save_route(record.model_dump())
    exported = export_route_to_json(record, result["route_id"])
    return {
        **result,
        "exported_json": exported,
        "exported_waypoints": {"file_name": exported.get("waypoint_file_name")},
    }


@app.post("/api/waypoints/parse")
def parse_waypoint_file(content: str = Body(..., media_type="text/plain")):
    """Parse a QGC WPL 110 file and expose both mission rows and map points."""
    try:
        mission_items = parse_qgc_waypoints(content)
        return {
            "format": QGC_WPL_HEADER,
            "fields": list(QGC_WPL_FIELDS),
            "mission_items": mission_items,
            "points": route_points_from_qgc_items(mission_items),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/routes")
def routes():
    return route_storage.list_routes()


@app.get("/api/routes/{route_id}")
def route(route_id: str):
    result = route_storage.get_route(route_id)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该航线记录")
    return result

@app.put("/api/routes/{route_id}")
def update_route(route_id: str, record: RouteRecord):
    payload = record.model_dump()
    payload["_update"] = True
    result = route_storage.save_route(payload, route_id)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该航线记录")
    exported = export_route_to_json(record, route_id)
    return {
        **result,
        "exported_json": exported,
        "exported_waypoints": {"file_name": exported.get("waypoint_file_name")},
    }

@app.delete("/api/routes/{route_id}", status_code=204)
def delete_route(route_id: str):
    if not route_storage.delete_route(route_id):
        raise HTTPException(status_code=404, detail="未找到该航线记录")
    delete_export_files_for_route(route_id)

@app.get("/api/exported-routes")
def list_exported_routes():
    files = []
    if EXPORT_DIR.exists():
        for file in EXPORT_DIR.glob("*.json"):
            files.append(_exported_route_record(file))
    files.sort(key=lambda x: x["timestamp"], reverse=True)
    return files

@app.get("/api/exported-routes/{file_name}")
def get_exported_route(file_name: str):
    file_path = _exported_route_path(file_name)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, media_type="application/json", filename=file_name)


@app.get("/api/exported-waypoints/{file_name}")
def get_exported_waypoint(file_name: str):
    file_path = _exported_waypoint_path(file_name)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="航点文件不存在")
    return FileResponse(file_path, media_type="text/plain; charset=utf-8", filename=file_name)

@app.put("/api/exported-routes/{file_name}/rename")
def rename_exported_route(file_name: str, request: ExportedRouteRenameRequest):
    file_path = _exported_route_path(file_name)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if request.file_name:
        target_path = _unique_export_file_path(request.file_name, exclude=file_path)
        if target_path != file_path:
            waypoint_path = file_path.with_suffix(".waypoints")
            file_path.rename(target_path)
            if waypoint_path.exists():
                waypoint_path.rename(target_path.with_suffix(".waypoints"))
        return _exported_route_record(target_path)
    elif request.route_name:
        route_name = request.route_name.strip()
        route_id = _export_route_payload_route_id(file_path)
        if not route_id:
            route_id = _unique_route_id_by_name(_exported_route_record(file_path).get("route_name"))
        if route_id:
            updated = route_storage.update_route_name(route_id, route_name)
            if updated is None:
                raise HTTPException(status_code=404, detail="未找到该航线记录")
            for item in _export_files_for_route(route_id):
                _update_export_route_name(item, route_name, route_id)
            _update_export_route_name(file_path, route_name, route_id)
        else:
            _update_export_route_name(file_path, route_name)
        return _exported_route_record(file_path)
    else:
        raise HTTPException(status_code=400, detail="必须提供新的 JSON 文件名")

@app.delete("/api/exported-routes/{file_name}", status_code=204)
def delete_exported_route(file_name: str):
    file_path = _exported_route_path(file_name)
    if file_path.exists():
        route_id = _export_route_payload_route_id(file_path)
        if not route_id:
            route_id = _unique_route_id_by_name(_exported_route_record(file_path).get("route_name"))
        try:
            file_path.with_suffix(".waypoints").unlink()
        except FileNotFoundError:
            pass
        file_path.unlink()
        if route_id:
            route_storage.delete_route(route_id)
            delete_export_files_for_route(route_id)
    else:
        raise HTTPException(status_code=404, detail="文件不存在")
