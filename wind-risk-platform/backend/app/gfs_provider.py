"""Read existing GFS GRIB2 files and normalize them for map visualization."""

from __future__ import annotations

import json
import os
import re
import contextlib
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock

import numpy as np

from .wind_provider import WindGrid

BEIJING_TZ = timezone(timedelta(hours=8))
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIRS = (
    REPOSITORY_ROOT / "data" / "gfs_hourly_windcheck",
    REPOSITORY_ROOT / "data" / "gdex_gfs_0p25_windcheck",
    REPOSITORY_ROOT / "data" / "gdex_gfs_0p25_global",
)
# A non-positive value preserves the source grid resolution. Set
# GFS_MAX_GRID_POINTS to a positive number only when explicit downsampling is wanted.
MAX_GRID_POINTS = int(os.getenv("GFS_MAX_GRID_POINTS", "0"))
LOW_LEVEL_HEIGHTS_M = (10, 20, 30, 40, 50, 80, 100)
INTERPOLATED_HEIGHTS_M = (200, 300, 500, 800, 1000, 1500, 2000, 3000)
SUPPORTED_HEIGHTS_M = (*LOW_LEVEL_HEIGHTS_M, *INTERPOLATED_HEIGHTS_M)
ECCODES_LOCK = RLock()

GDEX_NAME = re.compile(r"gfs\.0p25\.(\d{10})\.f(\d{3})\.grib2$", re.IGNORECASE)
REALTIME_NAME = re.compile(
    r"(?:\d{4}_)?gfs_(\d{8})_(\d{2})z_f(\d{3})_.+\.grib2$", re.IGNORECASE
)


@dataclass(frozen=True)
class GfsFile:
    """One available GFS cycle/forecast-hour GRIB2 file."""

    path: Path
    cycle: str
    cycle_bj: str
    forecast_hour: int
    valid_time: str
    valid_time_bj: str


def configured_data_dirs() -> tuple[Path, ...]:
    """Return configured GRIB2 search roots."""
    configured = os.getenv("GFS_DATA_DIRS")
    if not configured:
        return DEFAULT_DATA_DIRS
    return tuple(Path(item.strip()).expanduser().resolve() for item in configured.split(os.pathsep) if item.strip())


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_bj(value: datetime) -> str:
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M 北京时间")


def _parse_compact_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def _parse_utc_label(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)


def _parse_bj_label(value: str) -> datetime:
    normalized = value.replace("北京时间", "").strip()
    return datetime.strptime(normalized, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING_TZ)


def _current_utc() -> datetime:
    """Return the current UTC time, with a test override for diagnostics."""
    configured = os.getenv("GFS_NOW_UTC")
    if configured:
        text = configured.strip()
        try:
            if text.endswith("UTC"):
                return _parse_utc_label(text)
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError as exc:
            raise ValueError("GFS_NOW_UTC must be ISO time or 'YYYY-MM-DD HH:MM UTC'") from exc
    return datetime.now(timezone.utc)


def selectable_valid_time_floor() -> datetime:
    """Return the current-hour floor retained for diagnostics and compatibility."""
    now = _current_utc()
    return now.replace(minute=0, second=0, microsecond=0)


def is_selectable_valid_time(valid_time: str) -> bool:
    """Return True for every discovered time so historical files are testable."""
    _parse_utc_label(valid_time)
    return True


def is_current_or_future_valid_time(valid_time: str) -> bool:
    """Return True when a UTC valid-time label is not earlier than the current hour."""
    return _parse_utc_label(valid_time) >= selectable_valid_time_floor()


def _read_metadata(path: Path) -> dict:
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def describe_grib(path: Path) -> GfsFile | None:
    """Extract cycle and forecast hour from existing downloader file conventions."""
    metadata = _read_metadata(path)
    cycle_text = metadata.get("target_cycle_utc") or metadata.get("cycle_utc") or metadata.get("source_cycle_utc")
    forecast_hour = metadata.get("target_forecast_hour")
    if forecast_hour is None:
        forecast_hour = metadata.get("forecast_hour", metadata.get("source_forecast_hour"))

    if cycle_text is None or forecast_hour is None:
        match = GDEX_NAME.search(path.name)
        if match:
            cycle_text, forecast_hour = match.group(1), int(match.group(2))
        else:
            match = REALTIME_NAME.search(path.name)
            if not match:
                return None
            cycle_text, forecast_hour = f"{match.group(1)}{match.group(2)}", int(match.group(3))

    try:
        cycle_time = _parse_compact_utc(str(cycle_text))
        forecast_hour = int(forecast_hour)
    except (TypeError, ValueError):
        return None
    valid_time = cycle_time + timedelta(hours=forecast_hour)
    return GfsFile(
        path.resolve(),
        _format_utc(cycle_time),
        _format_bj(cycle_time),
        forecast_hour,
        _format_utc(valid_time),
        _format_bj(valid_time),
    )


@lru_cache(maxsize=1)
def discover_files() -> tuple[GfsFile, ...]:
    """Index all existing GRIB2 files under configured data directories."""
    by_selection: dict[tuple[str, int], GfsFile] = {}
    for root in configured_data_dirs():
        if not root.exists():
            continue
        for path in root.rglob("*.grib2"):
            item = describe_grib(path)
            if item:
                key = (item.cycle, item.forecast_hour)
                existing = by_selection.get(key)
                # Prefer an explicitly downloaded global wind-map file over a
                # point/subregion file for the same cycle and forecast hour.
                if existing is None or ("_global_" in item.path.name and "_global_" not in existing.path.name):
                    by_selection[key] = item
    return tuple(sorted(by_selection.values(), key=lambda item: (item.cycle, item.forecast_hour)))


def refresh_file_index() -> None:
    """Clear the cached file index after new downloads arrive."""
    with ECCODES_LOCK:
        discover_files.cache_clear()
        _get_grid_cached.cache_clear()


def availability() -> dict:
    """Return selections currently backed by discovered GFS files."""
    files = tuple(item for item in discover_files() if is_selectable_valid_time(item.valid_time))
    forecast_hours_by_cycle: dict[str, list[int]] = {}
    valid_time_options: dict[str, dict] = {}
    for item in files:
        forecast_hours_by_cycle.setdefault(item.cycle, []).append(item.forecast_hour)
        existing = valid_time_options.get(item.valid_time)
        option = {
            "label": item.valid_time_bj,
            "valid_time": item.valid_time,
            "cycle": item.cycle,
            "cycle_bj": item.cycle_bj,
            "forecast_hour": item.forecast_hour,
        }
        if existing is None or _parse_utc_label(item.cycle) > _parse_utc_label(existing["cycle"]):
            valid_time_options[item.valid_time] = option
    return {
        "cycles": sorted({item.cycle for item in files}),
        "forecast_hours": sorted({item.forecast_hour for item in files}),
        "forecast_hours_by_cycle": {
            cycle: sorted(set(hours)) for cycle, hours in forecast_hours_by_cycle.items()
        },
        "valid_times": sorted(valid_time_options.values(), key=lambda item: item["valid_time"]),
        "levels": [*[f"{height}m AGL" for height in SUPPORTED_HEIGHTS_M], AVERAGE_LAYER],
    }


def eccodes_runtime() -> dict:
    """Return ecCodes runtime diagnostics and fail clearly when definitions are unavailable."""
    try:
        import eccodes

        with ECCODES_LOCK:
            definition_path = eccodes.codes_definition_path()
            sample_path = eccodes.codes_samples_path()
            version = eccodes.codes_get_api_version()
            # Creating a sample forces ecCodes to verify that definitions are usable.
            handle = eccodes.codes_grib_new_from_samples("GRIB2")
            eccodes.codes_release(handle)
        return {
            "status": "ok",
            "version": version,
            "definition_path": definition_path,
            "sample_path": sample_path,
        }
    except Exception as exc:
        raise RuntimeError(
            "ecCodes runtime is unavailable or cannot find definitions. "
            "Start with '.\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8000'. "
            f"Original error: {exc}"
        ) from exc


def find_file(cycle: str, forecast_hour: int) -> GfsFile:
    """Find a GRIB2 file for an exact cycle and forecast hour."""
    for item in discover_files():
        if item.cycle == cycle and item.forecast_hour == forecast_hour:
            return item
    raise ValueError(f"找不到 cycle={cycle}, forecast_hour={forecast_hour} 的 GFS GRIB2 文件")


def find_file_by_valid_time(valid_time: str) -> GfsFile:
    """Find the best GRIB2 file by Beijing or UTC valid time label."""
    try:
        if "北京时间" in valid_time:
            target_utc = _parse_bj_label(valid_time).astimezone(timezone.utc)
        else:
            target_utc = _parse_utc_label(valid_time)
    except ValueError as exc:
        raise ValueError("valid_time must be 'YYYY-MM-DD HH:MM 北京时间' or 'YYYY-MM-DD HH:MM UTC'") from exc

    matches = [item for item in discover_files() if _parse_utc_label(item.valid_time) == target_utc]
    if not matches:
        raise ValueError(f"no GFS GRIB2 file for valid_time={valid_time}")
    return sorted(matches, key=lambda item: (_parse_utc_label(item.cycle), item.forecast_hour), reverse=True)[0]


def normalize_agl_level(level: str) -> tuple[str, int]:
    """Normalize any supported AGL label and return (label, height_m)."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*m", level.strip(), re.IGNORECASE)
    if not match:
        raise ValueError(f"不支持的高度层格式: {level}")
    height = int(round(float(match.group(1))))
    if height not in SUPPORTED_HEIGHTS_M:
        raise ValueError(f"不支持的 GFS 离地高度层: {height}m AGL")
    return f"{height}m AGL", height


AVERAGE_LAYER = "250-350m AGL average"


def is_average_layer(level: str) -> bool:
    return level.strip().lower() == AVERAGE_LAYER.lower()


def _pick_var(dataset, candidates: list[str]):
    for name in candidates:
        if name in dataset:
            return dataset[name]
    if len(dataset.data_vars) == 1:
        return dataset[next(iter(dataset.data_vars))]
    raise ValueError(f"无法识别风场变量；候选变量={candidates}，实际存在={list(dataset.data_vars)}")


def _open_height_var(path: Path, height: int, kind: str):
    try:
        import xarray as xr
        import cfgrib  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "reading GFS GRIB2 requires xarray, cfgrib and eccodes; install backend requirements"
        ) from exc

    short_names = [f"{height}{kind}", f"{kind}{height}", kind, f"{kind}grd"]
    candidates = [f"{kind}{height}", kind, f"{kind}grd"]
    filters = []
    for short_name in short_names:
        filters.extend(
            [
                {"typeOfLevel": "heightAboveGround", "level": height, "shortName": short_name},
                {
                    "typeOfLevel": "heightAboveGround",
                    "scaledValueOfFirstFixedSurface": height,
                    "shortName": short_name,
                },
            ]
        )
    filters.extend(
        [
            {"typeOfLevel": "heightAboveGround", "level": height},
            {"typeOfLevel": "heightAboveGround", "scaledValueOfFirstFixedSurface": height},
        ]
    )

    errors = []
    for filter_by_keys in filters:
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                dataset = xr.open_dataset(
                    path,
                    engine="cfgrib",
                    backend_kwargs={"filter_by_keys": filter_by_keys, "indexpath": ""},
                )
            if dataset.data_vars:
                result = _pick_var(dataset, candidates).load()
                dataset.close()
                return result
            dataset.close()
        except Exception as exc:  # cfgrib raises several backend-specific exception types
            errors.append(str(exc))
    raise ValueError(f"GRIB2 does not contain {height}m AGL {kind.upper()} wind: {errors[-1] if errors else 'unknown'}")


def _open_dataset(path: Path, filter_by_keys: dict):
    import xarray as xr

    with contextlib.redirect_stderr(io.StringIO()):
        return xr.open_dataset(
            path,
            engine="cfgrib",
            backend_kwargs={"filter_by_keys": filter_by_keys, "indexpath": ""},
        )


def _open_isobaric_dataset(path: Path):
    errors = []
    for level_type in ("isobaricInhPa", "isobaricInPa"):
        try:
            dataset = _open_dataset(path, {"typeOfLevel": level_type})
            if {"u", "v"}.intersection(dataset.data_vars) and {"gh", "hgt"}.intersection(dataset.data_vars):
                return dataset
            dataset.close()
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError(
        "GRIB2 文件不包含用于 AGL 插值的等压面 U/V/HGT 变量；"
        "如果使用的是轻量级 --wind-map-only 文件，则无法获取高空数据。 "
        f"最后一次错误: {errors[-1] if errors else 'unknown'}"
    )


def _open_surface_height(path: Path):
    errors = []
    for filters in ({"typeOfLevel": "surface"}, {"typeOfLevel": "heightAboveGround", "level": 0}):
        try:
            dataset = _open_dataset(path, filters)
            try:
                terrain = _pick_var(dataset, ["orog", "gh", "hgt"]).load()
                dataset.close()
                return terrain
            except ValueError:
                dataset.close()
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError(f"GRIB2 lacks surface height/orography required for AGL interpolation: {errors[-1] if errors else 'unknown'}")


def _coord_name(data_array, candidates: tuple[str, ...]) -> str:
    for name in candidates:
        if name in data_array.coords:
            return name
    raise ValueError(f"missing coordinate {candidates}; actual={list(data_array.coords)}")


def normalize_grid(
    u_data,
    v_data,
    bbox: tuple[float, float, float, float] | None = None,
    max_points: int = MAX_GRID_POINTS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Normalize DataArrays to lon ascending, lat north-to-south, regular 2-D arrays."""
    lat_name = _coord_name(u_data, ("latitude", "lat"))
    lon_name = _coord_name(u_data, ("longitude", "lon"))
    v_lat_name = _coord_name(v_data, ("latitude", "lat"))
    v_lon_name = _coord_name(v_data, ("longitude", "lon"))

    u_data = u_data.squeeze(drop=True)
    v_data = v_data.squeeze(drop=True)
    if set(u_data.dims) != {lat_name, lon_name} or set(v_data.dims) != {v_lat_name, v_lon_name}:
        raise ValueError(f"U/V 必须是 2D 的经纬度网格；U={u_data.dims}, V={v_data.dims}")

    u = np.asarray(u_data.transpose(lat_name, lon_name).values, dtype=float)
    v = np.asarray(v_data.transpose(v_lat_name, v_lon_name).values, dtype=float)
    lats = np.asarray(u_data[lat_name].values, dtype=float).reshape(-1)
    lons = np.asarray(u_data[lon_name].values, dtype=float).reshape(-1)
    v_lats = np.asarray(v_data[v_lat_name].values, dtype=float).reshape(-1)
    v_lons = np.asarray(v_data[v_lon_name].values, dtype=float).reshape(-1)

    if u.shape != v.shape or len(lats) != u.shape[0] or len(lons) != u.shape[1]:
        raise ValueError(f"U/V 数据和坐标维度大小不一致: U={u.shape}, V={v.shape}, lat={len(lats)}, lon={len(lons)}")
    if not np.allclose(lats, v_lats) or not np.allclose(lons, v_lons):
        raise ValueError("U/V 的经度或纬度坐标不一致")

    lons = ((lons + 180.0) % 360.0) - 180.0
    lon_order = np.argsort(lons)
    lat_order = np.argsort(lats)[::-1]
    lons, lats = lons[lon_order], lats[lat_order]
    u, v = u[np.ix_(lat_order, lon_order)], v[np.ix_(lat_order, lon_order)]

    unique_lons, unique_indices = np.unique(np.round(lons, 10), return_index=True)
    lons = unique_lons
    u, v = u[:, unique_indices], v[:, unique_indices]

    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        lon_mask = (lons >= min_lon) & (lons <= max_lon)
        lat_mask = (lats >= min_lat) & (lats <= max_lat)
        if not lon_mask.any() or not lat_mask.any():
            raise ValueError("查询范围(bbox)与该 GFS 网格没有交集")
        lons, lats = lons[lon_mask], lats[lat_mask]
        u, v = u[lat_mask][:, lon_mask], v[lat_mask][:, lon_mask]

    stride = max(1, int(np.ceil(np.sqrt(u.size / max_points)))) if max_points > 0 else 1
    lons, lats, u, v = lons[::stride], lats[::stride], u[::stride, ::stride], v[::stride, ::stride]
    if len(lons) < 2 or len(lats) < 2:
        raise ValueError("selected GFS grid must contain at least 2 longitude and 2 latitude points")
    if not np.all(np.diff(lons) > 0) or not np.all(np.diff(lats) < 0):
        raise ValueError("failed to normalize longitude/latitude directions")
    if not np.allclose(np.diff(lons), np.diff(lons)[0], rtol=1e-5, atol=1e-7):
        raise ValueError("longitude coordinate is not a regular grid")
    if not np.allclose(np.diff(lats), np.diff(lats)[0], rtol=1e-5, atol=1e-7):
        raise ValueError("latitude coordinate is not a regular grid")
    return lons, lats, u, v


def _crop_data_array(data_array, bbox: tuple[float, float, float, float] | None):
    if not bbox:
        return data_array
    lat_name = _coord_name(data_array, ("latitude", "lat"))
    lon_name = _coord_name(data_array, ("longitude", "lon"))
    min_lon, min_lat, max_lon, max_lat = bbox
    native_lons = np.asarray(data_array[lon_name].values, dtype=float)
    selector = {}
    selector[lon_name] = native_lons[(native_lons >= (min_lon % 360)) & (native_lons <= (max_lon % 360))]
    lats = np.asarray(data_array[lat_name].values, dtype=float)
    selector[lat_name] = lats[(lats >= min_lat) & (lats <= max_lat)]
    if len(selector[lon_name]) == 0 or len(selector[lat_name]) == 0:
        raise ValueError("查询范围(bbox)与该 GFS 网格没有交集")

    return data_array.sel(selector)


def _pressure_coord(data_array) -> str:
    for name in ("isobaricInhPa", "isobaricInPa", "level"):
        if name in data_array.coords:
            return name
    raise ValueError(f"缺少等压面高度坐标；可用坐标={list(data_array.coords)}")


def _interpolate_to_agl(path: Path, height: int, bbox: tuple[float, float, float, float] | None):
    dataset = _open_isobaric_dataset(path)
    try:
        gh = _pick_var(dataset, ["gh", "hgt"])
        u = _pick_var(dataset, ["u", "ugrd"])
        v = _pick_var(dataset, ["v", "vgrd"])
        terrain = _open_surface_height(path)

        gh = _crop_data_array(gh, bbox).load()
        u = _crop_data_array(u, bbox).load()
        v = _crop_data_array(v, bbox).load()
        terrain = _crop_data_array(terrain, bbox).load()

        lat_name = _coord_name(gh, ("latitude", "lat"))
        lon_name = _coord_name(gh, ("longitude", "lon"))
        coord = _pressure_coord(gh)
        gh = gh.transpose(coord, lat_name, lon_name)
        u = u.transpose(coord, lat_name, lon_name)
        v = v.transpose(coord, lat_name, lon_name)

        height_agl = np.asarray(gh.values, dtype=float) - np.asarray(terrain.values, dtype=float)
        u_values = np.asarray(u.values, dtype=float)
        v_values = np.asarray(v.values, dtype=float)
        ny, nx = height_agl.shape[1:]
        out_u = np.full((ny, nx), np.nan, dtype=float)
        out_v = np.full((ny, nx), np.nan, dtype=float)

        for row in range(ny):
            for col in range(nx):
                z = height_agl[:, row, col]
                uu = u_values[:, row, col]
                vv = v_values[:, row, col]
                good = np.isfinite(z) & np.isfinite(uu) & np.isfinite(vv)
                if good.sum() < 2:
                    continue
                order = np.argsort(z[good])
                z_sorted = z[good][order]
                if height < z_sorted[0] or height > z_sorted[-1]:
                    continue
                out_u[row, col] = np.interp(height, z_sorted, uu[good][order])
                out_v[row, col] = np.interp(height, z_sorted, vv[good][order])

        u_2d = gh.isel({coord: 0}, drop=True).copy(data=out_u)
        v_2d = gh.isel({coord: 0}, drop=True).copy(data=out_v)
        return u_2d, v_2d
    finally:
        dataset.close()


@lru_cache(maxsize=24)
def _get_grid_cached(
    cycle: str,
    forecast_hour: int,
    level: str,
    bbox: tuple[float, float, float, float] | None,
) -> WindGrid:
    """Read one existing GFS file and return a normalized regular wind grid."""
    average_layer = is_average_layer(level)
    normalized_level, height = (AVERAGE_LAYER, None) if average_layer else normalize_agl_level(level)
    item = find_file(cycle, forecast_hour)
    try:
        if average_layer:
            samples = [_interpolate_to_agl(item.path, sample_height, bbox) for sample_height in (250, 300, 350)]
            u_data = samples[0][0].copy(data=(samples[0][0].values + 4 * samples[1][0].values + samples[2][0].values) / 6)
            v_data = samples[0][1].copy(data=(samples[0][1].values + 4 * samples[1][1].values + samples[2][1].values) / 6)
        elif height in LOW_LEVEL_HEIGHTS_M:
            u_data = _open_height_var(item.path, height, "u")
            v_data = _open_height_var(item.path, height, "v")
        else:
            u_data, v_data = _interpolate_to_agl(item.path, height, bbox)
    except ValueError as exc:
        raise ValueError(f"failed to read {item.path.name} at {normalized_level}: {exc}") from exc
    lons, lats, u, v = normalize_grid(u_data, v_data, bbox)
    return WindGrid(
        lons=lons,
        lats=lats,
        u=u,
        v=v,
        cycle=item.cycle,
        forecast_hour=item.forecast_hour,
        level=normalized_level,
        valid_time=item.valid_time,
        source=f"GFS GRIB2: {item.path.name}",
        cycle_bj=item.cycle_bj,
        valid_time_bj=item.valid_time_bj,
    )


def get_grid(
    cycle: str,
    forecast_hour: int,
    level: str,
    bbox: tuple[float, float, float, float] | None,
) -> WindGrid:
    """Serialize cache misses because ecCodes definitions parsing is not thread-safe."""
    with ECCODES_LOCK:
        return _get_grid_cached(cycle, forecast_hour, level, bbox)


def get_grid_by_valid_time(
    valid_time: str,
    level: str,
    bbox: tuple[float, float, float, float] | None,
) -> WindGrid:
    """Load the best available grid for a UTC or Beijing valid-time label."""
    with ECCODES_LOCK:
        item = find_file_by_valid_time(valid_time)
        return _get_grid_cached(item.cycle, item.forecast_hour, level, bbox)
