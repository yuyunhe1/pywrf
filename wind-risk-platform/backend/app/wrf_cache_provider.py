"""Read preprocessed WRF platform cache files, with optional SFTP mirroring."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock

import numpy as np

from .gfs_provider import BEIJING_TZ, is_selectable_valid_time
from .wind_provider import WindGrid


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = REPOSITORY_ROOT / "data" / "wrf_platform_cache"
REMOTE_INDEX = "index.json"
SYNC_LOCK = RLock()
AVERAGE_LAYER = "250-350m AGL average"


def cache_dir() -> Path:
    """Return the local WRF cache mirror directory."""
    return Path(os.getenv("WRF_CACHE_DIR", str(DEFAULT_CACHE_DIR))).expanduser().resolve()


def remote_configured() -> bool:
    """Return True when the backend should mirror cache files through SFTP."""
    return bool(os.getenv("WRF_CACHE_REMOTE_HOST") and os.getenv("WRF_CACHE_REMOTE_DIR"))


def _format_bj(value: str) -> str:
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M 北京时间")


def _connect_sftp():
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError("SFTP cache access requires paramiko; install backend requirements.txt") from exc

    host = os.environ["WRF_CACHE_REMOTE_HOST"]
    port = int(os.getenv("WRF_CACHE_REMOTE_PORT", "22"))
    username = os.getenv("WRF_CACHE_REMOTE_USER")
    password = os.getenv("WRF_CACHE_REMOTE_PASSWORD")
    key_filename = os.getenv("WRF_CACHE_REMOTE_KEY")
    if not username:
        raise RuntimeError("WRF_CACHE_REMOTE_USER is required when SFTP cache is enabled")

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if os.getenv("WRF_CACHE_REMOTE_ALLOW_UNKNOWN_HOST", "1") == "1":
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        port=port,
        username=username,
        password=password,
        key_filename=key_filename,
        timeout=int(os.getenv("WRF_CACHE_REMOTE_TIMEOUT", "30")),
    )
    return client, client.open_sftp()


def _remote_path(relative_path: str) -> str:
    root = os.environ["WRF_CACHE_REMOTE_DIR"].rstrip("/")
    normalized = relative_path.replace("\\", "/")
    return f"{root}/{normalized}"


def _download_remote_file(relative_path: str, force: bool = False) -> Path:
    local_path = cache_dir() / relative_path
    if local_path.exists() and not force:
        return local_path
    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
    client, sftp = _connect_sftp()
    try:
        sftp.get(_remote_path(relative_path), str(tmp_path))
        tmp_path.replace(local_path)
    finally:
        sftp.close()
        client.close()
        if tmp_path.exists():
            tmp_path.unlink()
    return local_path


def sync_index(force: bool = False) -> None:
    """Mirror remote index.json when SFTP is configured."""
    if not remote_configured():
        return
    with SYNC_LOCK:
        _download_remote_file(REMOTE_INDEX, force=force)
        load_index.cache_clear()
        _get_grid_cached.cache_clear()


def ensure_cache_file(relative_path: str) -> Path:
    """Return a local cache file, downloading it from SFTP on demand."""
    local_path = cache_dir() / relative_path
    if local_path.exists():
        return local_path
    if remote_configured():
        return _download_remote_file(relative_path)
    raise ValueError(f"WRF 降尺度缓存文件不存在: {local_path}")


@lru_cache(maxsize=1)
def load_index() -> dict:
    """Load the local WRF cache index, mirroring it once when configured."""
    if remote_configured() and not (cache_dir() / REMOTE_INDEX).exists():
        sync_index(force=True)
    path = cache_dir() / REMOTE_INDEX
    if not index_path.is_file():
        raise ValueError(f"找不到 WRF 缓存索引文件: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_cache() -> None:
    """Refresh local index and cached grids."""
    with SYNC_LOCK:
        if remote_configured():
            _download_remote_file(REMOTE_INDEX, force=True)
        load_index.cache_clear()
        _get_grid_cached.cache_clear()


def availability() -> dict:
    """Return selectable WRF cache times and levels."""
    if remote_configured() and os.getenv("WRF_CACHE_AUTO_SYNC_INDEX", "1") == "1":
        sync_index(force=True)
    index = load_index()
    valid_times = [
        item for item in index.get("valid_times", [])
        if is_selectable_valid_time(item["valid_time"])
    ]
    cycles = sorted({item["cycle"] for item in valid_times})
    forecast_hours = sorted({int(item["forecast_hour"]) for item in valid_times})
    by_cycle = {
        cycle: sorted({int(item["forecast_hour"]) for item in valid_times if item["cycle"] == cycle})
        for cycle in cycles
    }
    levels = list(index.get("levels", []))
    available_heights = [
        float(level.lower().replace("agl", "").replace("m", "").strip())
        for level in levels
        if level.lower().replace("agl", "").replace("m", "").strip().replace(".", "", 1).isdigit()
    ]
    if available_heights and min(available_heights) <= 250 and max(available_heights) >= 350:
        levels.append(AVERAGE_LAYER)
    return {
        "cycles": cycles,
        "forecast_hours": forecast_hours,
        "forecast_hours_by_cycle": by_cycle,
        "valid_times": sorted(valid_times, key=lambda item: item["valid_time"]),
        "levels": levels,
    }


def _records() -> list[dict]:
    return list(load_index().get("files", []))


def _find_record(cycle: str, forecast_hour: int) -> dict:
    matches = [
        item for item in _records()
        if item["cycle"] == cycle and int(item["forecast_hour"]) == int(forecast_hour)
    ]
    if cycle not in data:
        raise ValueError(f"找不到 cycle={cycle}, forecast_hour={forecast_hour} 的 WRF 缓存文件")
    return matches[-1]


def _find_record_by_valid_time(valid_time: str) -> dict:
    if "北京时间" in valid_time:
        normalized = valid_time.replace("北京时间", "").strip()
        dt = datetime.strptime(normalized, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING_TZ)
        valid_time = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    matches = [item for item in _records() if item["valid_time"] == valid_time]
    if not matches:
        raise ValueError(f"no WRF cache file for valid_time={valid_time}")
    return sorted(matches, key=lambda item: (item["cycle"], int(item["forecast_hour"])), reverse=True)[0]


def _level_index(levels_m: np.ndarray, level: str) -> tuple[str, int]:
    text = level.strip().lower().replace("agl", "").replace(" ", "")
    if not text.endswith("m"):
        raise ValueError(f"不支持的 WRF 缓存高度层格式: {level}")
    height = int(round(float(text[:-1])))
    matches = np.where(np.isclose(levels_m.astype(float), float(height), atol=0.1))[0]
    if len(matches) == 0:
        raise ValueError(f"WRF 缓存数据中不包含 {height}m AGL 的高度层")
    return f"{height}m AGL", int(matches[0])


def _average_250_350m(levels_m: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Linearly interpolate U/V at 250, 300 and 350 m then average the layer."""
    order = np.argsort(levels_m)
    heights = levels_m[order]
    if heights[0] > 250 or heights[-1] < 350:
        raise ValueError("WRF cache does not cover the 250-350m AGL average layer")
    ordered = values[order]
    samples = [
        np.apply_along_axis(lambda column: np.interp(height, heights, column), 0, ordered)
        for height in (250, 300, 350)
    ]
    # Simpson's rule gives the mean through the requested 100 m layer.
    return (samples[0] + 4 * samples[1] + samples[2]) / 6


def _crop(lons: np.ndarray, lats: np.ndarray, u: np.ndarray, v: np.ndarray, bbox):
    if not bbox:
        return lons, lats, u, v
    min_lon, min_lat, max_lon, max_lat = bbox
    eps = 1e-5
    lon_mask = (lons >= min_lon - eps) & (lons <= max_lon + eps)
    lat_mask = (lats >= min_lat - eps) & (lats <= max_lat + eps)
    if not lon_mask.any() or not lat_mask.any():
        raise ValueError("查询范围(bbox)与该 WRF 缓存网格没有交集")
    return lons[lon_mask], lats[lat_mask], u[lat_mask][:, lon_mask], v[lat_mask][:, lon_mask]


@lru_cache(maxsize=48)
def _get_grid_cached(record_path: str, level: str, bbox):
    path = ensure_cache_file(record_path)
    with np.load(path, allow_pickle=False) as data:
        lons = np.asarray(data["lons"], dtype=float)
        lats = np.asarray(data["lats"], dtype=float)
        levels_m = np.asarray(data["levels_m"], dtype=float)
        if level.strip().lower() == AVERAGE_LAYER.lower():
            normalized_level = AVERAGE_LAYER
            u = _average_250_350m(levels_m, np.asarray(data["u"], dtype=float))
            v = _average_250_350m(levels_m, np.asarray(data["v"], dtype=float))
        else:
            normalized_level, index = _level_index(levels_m, level)
            u = np.asarray(data["u"][index], dtype=float)
            v = np.asarray(data["v"][index], dtype=float)
        cycle = str(data["cycle_utc"].item())
        valid_time = str(data["valid_time_utc"].item())
        forecast_hour = int(data["forecast_hour"].item())

    lons, lats, u, v = _crop(lons, lats, u, v, bbox)
    return WindGrid(
        lons=lons,
        lats=lats,
        u=u,
        v=v,
        cycle=cycle,
        forecast_hour=forecast_hour,
        level=normalized_level,
        valid_time=valid_time,
        source=f"WRF cache: {Path(record_path).name}",
        cycle_bj=_format_bj(cycle),
        valid_time_bj=_format_bj(valid_time),
    )


def get_grid(cycle: str, forecast_hour: int, level: str, bbox) -> WindGrid:
    """Load one WRF cache grid by cycle and forecast hour."""
    record = _find_record(cycle, forecast_hour)
    return _get_grid_cached(record["path"], level, bbox)


def get_grid_by_valid_time(valid_time: str, level: str, bbox) -> WindGrid:
    """Load one WRF cache grid by UTC or Beijing valid-time label."""
    record = _find_record_by_valid_time(valid_time)
    return _get_grid_cached(record["path"], level, bbox)


def diagnostics() -> dict:
    """Return WRF cache provider diagnostics."""
    return {
        "mode": "wrf_cache",
        "cache_dir": str(cache_dir()),
        "remote_enabled": remote_configured(),
    }
