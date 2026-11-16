"""Read preprocessed WRF platform cache files, with optional SFTP mirroring."""

from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic
from zipfile import BadZipFile

import numpy as np

from .gfs_provider import BEIJING_TZ, _current_utc, is_selectable_valid_time
from .wind_provider import WindGrid
from .wrf_native_schema import NATIVE_WRF_TIME_MODEL, native_required, minute_of, npz_metadata, validate_index
from .wind_cache import bounded_grid_cache, decoded_cache_budget
from .planning_limits import integer_setting


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "wrf_platform_cache_10min"
REMOTE_INDEX = "index.json"
SYNC_LOCK = RLock()
INDEX_SYNC_STATE_LOCK = RLock()
INDEX_SYNC_THREAD: Thread | None = None
INDEX_SYNC_LAST_STARTED: float | None = None
POLL_THREAD: Thread | None = None
POLL_STOP = Event()
INDEX_SYNC_STATUS = {
    "running": False,
    "last_success": None,
    "last_error": None,
    "last_started": None,
    "next_check_at": None,
    "latest_cycle": None,
    "downloaded": 0,
}
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


def _remote_key_filename() -> str | None:
    configured = os.getenv("WRF_CACHE_REMOTE_KEY")
    if configured:
        return configured
    for name in ("pywrf_server_ed25519", "gdex_proxy_ed25519"):
        candidate = Path.home() / ".ssh" / name
        if candidate.is_file():
            return str(candidate)
    return None


def _connect_sftp():
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError("SFTP cache access requires paramiko; install backend requirements.txt") from exc

    host = os.environ["WRF_CACHE_REMOTE_HOST"]
    port = int(os.getenv("WRF_CACHE_REMOTE_PORT", "22"))
    username = os.getenv("WRF_CACHE_REMOTE_USER")
    password = os.getenv("WRF_CACHE_REMOTE_PASSWORD")
    key_filename = _remote_key_filename()
    if not username:
        raise RuntimeError("WRF_CACHE_REMOTE_USER is required when SFTP cache is enabled")

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if os.getenv("WRF_CACHE_REMOTE_ALLOW_UNKNOWN_HOST", "1") == "1":
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    timeout = int(os.getenv("WRF_CACHE_REMOTE_TIMEOUT", "30"))
    try:
        client.connect(
            host,
            port=port,
            username=username,
            password=password,
            key_filename=key_filename,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
        )
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(timeout)
        return client, sftp
    except Exception:
        client.close()
        raise


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
        if relative_path == REMOTE_INDEX:
            # The producer may still be writing the index. Keep the last good
            # local copy if this attempt retrieves incomplete JSON.
            json.loads(tmp_path.read_text(encoding="utf-8"))
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
    if native_required():
        return _sync_native_cycle()
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
        with SYNC_LOCK:
            return _download_remote_file(relative_path)
    raise ValueError(f"WRF 降尺度缓存文件不存在: {local_path}")


@lru_cache(maxsize=1)
def load_index() -> dict:
    """Load the local index; remote synchronization runs in the background."""
    path = cache_dir() / REMOTE_INDEX
    if not path.is_file():
        raise ValueError(f"找不到 WRF 缓存索引文件: {path}")
    index=json.loads(path.read_text(encoding="utf-8"))
    if native_required(): validate_index(index)
    return index


def refresh_cache() -> None:
    """Refresh local selections and request a non-blocking remote sync."""
    load_index.cache_clear()
    _get_grid_cached.cache_clear()
    schedule_index_sync(force=True)


def _sync_interval() -> float:
    try:
        return max(0.0, float(os.getenv("WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS", "600")))
    except ValueError:
        return 600.0


def _index_sync_status() -> dict:
    """Return a copy of the non-blocking remote index synchronization state."""
    with INDEX_SYNC_STATE_LOCK:
        return {
            **INDEX_SYNC_STATUS,
            "polling": POLL_THREAD is not None and POLL_THREAD.is_alive(),
            "interval_seconds": _sync_interval(),
        }


def _run_background_index_sync() -> None:
    """Discover and mirror new products without delaying local availability."""
    global INDEX_SYNC_LAST_STARTED
    try:
        result = sync_latest_cycle(force_index=True)
    except Exception as exc:
        with INDEX_SYNC_STATE_LOCK:
            INDEX_SYNC_STATUS["last_error"] = str(exc)
        print(f"[WRF_CACHE] background sync failed: {exc}", flush=True)
    else:
        with INDEX_SYNC_STATE_LOCK:
            INDEX_SYNC_STATUS["last_success"] = datetime.now(timezone.utc).isoformat()
            INDEX_SYNC_STATUS["last_error"] = None
            INDEX_SYNC_STATUS.update({
                key: result.get(key) for key in ("latest_cycle", "downloaded")
            })
        print(f"[WRF_CACHE] background sync: {result}", flush=True)
        from .wind_warmup import schedule
        schedule()
    finally:
        with INDEX_SYNC_STATE_LOCK:
            INDEX_SYNC_STATUS["running"] = False
            # Retry ten minutes after completion, including failed connections.
            INDEX_SYNC_STATUS["next_check_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=_sync_interval())
            ).isoformat()
            INDEX_SYNC_LAST_STARTED = monotonic()


def schedule_index_sync(force: bool = False) -> dict:
    """Schedule discovery and download of the latest cycle, without overlap."""
    global INDEX_SYNC_THREAD, INDEX_SYNC_LAST_STARTED

    if not remote_configured() or os.getenv("WRF_CACHE_AUTO_SYNC_INDEX", "1") != "1":
        return _index_sync_status()

    interval = _sync_interval()
    now = monotonic()
    with INDEX_SYNC_STATE_LOCK:
        if INDEX_SYNC_THREAD is not None and INDEX_SYNC_THREAD.is_alive():
            return _index_sync_status()
        if not force and INDEX_SYNC_LAST_STARTED is not None and now - INDEX_SYNC_LAST_STARTED < interval:
            return _index_sync_status()
        INDEX_SYNC_LAST_STARTED = now
        INDEX_SYNC_STATUS["running"] = True
        INDEX_SYNC_STATUS["last_started"] = datetime.now(timezone.utc).isoformat()
        INDEX_SYNC_STATUS["next_check_at"] = None
        INDEX_SYNC_THREAD = Thread(
            target=_run_background_index_sync,
            name="wrf-cache-index-sync",
            daemon=True,
        )
        INDEX_SYNC_THREAD.start()
        return _index_sync_status()


def _poll_remote_cache(stop: Event) -> None:
    """Keep querying even when no browser is open or a previous query failed."""
    while not stop.is_set():
        with INDEX_SYNC_STATE_LOCK:
            if INDEX_SYNC_STATUS["running"]:
                delay = 10.0
            elif INDEX_SYNC_LAST_STARTED is None:
                delay = max(1.0, _sync_interval())
            else:
                delay = max(0.1, _sync_interval() - (monotonic() - INDEX_SYNC_LAST_STARTED))
        if stop.wait(delay):
            return
        schedule_index_sync()


def start_polling(sync_on_startup: bool = True) -> None:
    global POLL_THREAD, POLL_STOP
    if not remote_configured() or os.getenv("WRF_CACHE_AUTO_SYNC_INDEX", "1") != "1":
        return
    with INDEX_SYNC_STATE_LOCK:
        if POLL_THREAD is not None and POLL_THREAD.is_alive():
            return
        POLL_STOP = Event()
        POLL_THREAD = Thread(target=_poll_remote_cache, args=(POLL_STOP,), name="wrf-cache-poll", daemon=True)
        if sync_on_startup:
            schedule_index_sync(force=True)
        POLL_THREAD.start()


def stop_polling() -> None:
    POLL_STOP.set()


def sync_latest_cycle(force_index: bool = True) -> dict:
    """Download the latest remote WRF cache cycle into the local mirror directory."""
    if not remote_configured():
        return {"enabled": False, "message": "remote WRF cache is not configured"}
    if native_required():
        return _sync_native_cycle()
    with SYNC_LOCK:
        _download_remote_file(REMOTE_INDEX, force=force_index)
        load_index.cache_clear()
        _get_grid_cached.cache_clear()
        index = load_index()
        files = list(index.get("files", []))
        if not files:
            return {"enabled": True, "latest_cycle": None, "downloaded": 0, "skipped": 0}

        latest_cycle = max(item["cycle"] for item in files)
        target = _lookahead_target_label()
        latest_files = sorted(
            (item for item in files if item.get("cycle") == latest_cycle),
            key=lambda item: (item.get("valid_time") != target, int(item.get("forecast_hour", 0))),
        )
        downloaded = 0
        skipped = 0
        for item in latest_files:
            relative_path = item.get("path")
            if not relative_path:
                continue
            local_path = cache_dir() / relative_path
            if local_path.is_file():
                skipped += 1
                continue
            _download_remote_file(relative_path, force=False)
            downloaded += 1
        load_index.cache_clear()
        _get_grid_cached.cache_clear()
        return {
            "enabled": True,
            "latest_cycle": latest_cycle,
            "files": len(latest_files),
            "downloaded": downloaded,
            "skipped": skipped,
        }


def _sync_native_cycle():
    from .wrf_native_sync import sync
    import sys
    return sync(sys.modules[__name__])


def availability() -> dict:
    """Return selectable WRF cache times and levels."""
    remote_sync = schedule_index_sync()
    index = load_index() if (cache_dir() / REMOTE_INDEX).is_file() else {}
    expose_remote_files = (
        remote_configured()
        and os.getenv("WRF_CACHE_EXPOSE_REMOTE_FILES", "0").strip().lower() in {"1", "true", "yes", "on"}
    )
    files = [
        item for item in index.get("files", [])
        if expose_remote_files or (cache_dir() / item["path"]).is_file()
    ]
    # Remote valid_times only names the newest cycle. Build from local records
    # so an older downloaded cycle stays usable while its replacement arrives.
    by_valid = {}
    for item in files:
        if not is_selectable_valid_time(item["valid_time"]):
            continue
        previous = by_valid.get(item["valid_time"])
        if previous is None or item["cycle"] > previous["cycle"]:
            by_valid[item["valid_time"]] = {
                "label": item.get("valid_time_bj") or item.get("label") or _format_bj(item["valid_time"]),
                "valid_time": item["valid_time"],
                "cycle": item["cycle"],
                "cycle_bj": item.get("cycle_bj") or _format_bj(item["cycle"]),
                "forecast_hour": float(item["forecast_hour"]),
                "forecast_minutes": item.get('forecast_minutes'),
            }
    valid_times = list(by_valid.values())
    cycles = sorted({item["cycle"] for item in valid_times})
    forecast_hours = sorted({float(item["forecast_hour"]) for item in valid_times})
    by_cycle = {
        cycle: sorted({float(item["forecast_hour"]) for item in valid_times if item["cycle"] == cycle})
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
        "remote_sync": remote_sync,
        "remote_files_exposed": expose_remote_files,
        "lookahead": lookahead_status(files),
        "wind_time_model": index.get('wind_time_model'),
        "output_interval_seconds":index.get('output_interval_seconds'),
    }


def _lookahead_target_label() -> str:
    target = _current_utc() + timedelta(hours=3)
    interval=10 if native_required() else 60
    hour = target.replace(minute=(target.minute//interval)*interval, second=0, microsecond=0)
    if target != hour:
        hour += timedelta(minutes=interval)
    return hour.strftime("%Y-%m-%d %H:%M UTC")


def lookahead_status(files: list[dict]) -> dict:
    """Report coverage of the first output frame at least three hours ahead."""
    label = _lookahead_target_label()
    local_files = [item for item in files if (cache_dir() / item["path"]).is_file()]
    matches = [item for item in local_files if item["valid_time"] == label]
    return {
        "hours": 3,
        "target_valid_time": label,
        "target_valid_time_bj": _format_bj(label),
        "available": bool(matches),
        "cycle": max((item["cycle"] for item in matches), default=None),
        "latest_local_cycle": max((item["cycle"] for item in local_files), default=None),
        "latest_local_valid_time": max((item["valid_time"] for item in local_files), default=None),
    }


def _records() -> list[dict]:
    return list(load_index().get("files", []))


def _find_record(cycle: str, forecast_hour: float) -> dict:
    matches = [
        item for item in _records()
        if item["cycle"] == cycle and abs(minute_of(item)-float(forecast_hour)*60)<1e-6
    ]
    if not matches:
        if native_required():
            raise ValueError(f"该批次或时次没有原生 10 分钟 WRF 产物：{cycle}, 预报 {forecast_hour*60:g} 分钟；不使用小时数据插值补齐")
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
    local_matches = [item for item in matches if (cache_dir() / item["path"]).is_file()]
    return max(local_matches or matches, key=lambda item: (item["cycle"], minute_of(item)))


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
    samples = []
    for height in (250, 300, 350):
        # Match np.interp's exact-height and last-duplicate behaviour. All
        # columns have the same AGL axis, so no Python loop over grid cells.
        right = int(np.searchsorted(heights, height, side='right'))
        left = right - 1
        if heights[left] == height:
            samples.append(ordered[left])
        else:
            slope = (ordered[right] - ordered[left]) / (heights[right] - heights[left])
            samples.append(ordered[left] + slope * (height - heights[left]))
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


def _crop_scalar(lons: np.ndarray, lats: np.ndarray, values: np.ndarray | None, bbox):
    if values is None:
        return None
    if not bbox:
        return values
    min_lon, min_lat, max_lon, max_lat = bbox
    eps = 1e-5
    lon_mask = (lons >= min_lon - eps) & (lons <= max_lon + eps)
    lat_mask = (lats >= min_lat - eps) & (lats <= max_lat + eps)
    if not lon_mask.any() or not lat_mask.any():
        return None
    return values[lat_mask][:, lon_mask]


def _pick_terrain_array(data) -> np.ndarray | None:
    for key in ("hgt_surface", "hgt_surface_m", "terrain", "terrain_height", "elevation", "HGT"):
        if key in data.files:
            terrain = np.asarray(data[key], dtype=float)
            while terrain.ndim > 2:
                terrain = terrain[0]
            return terrain
    return None


@bounded_grid_cache(max_bytes=decoded_cache_budget())
def _get_grid_cached(record_path: str, level: str, bbox, local_revision=None):
    path = cache_dir() / record_path if local_revision is not None else ensure_cache_file(record_path)
    with np.load(path, allow_pickle=False) as data:
        lons = np.asarray(data["lons"], dtype=float)
        lats = np.asarray(data["lats"], dtype=float)
        levels_m = np.asarray(data["levels_m"], dtype=float)
        terrain = _pick_terrain_array(data)
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
        metadata=npz_metadata(data)
        forecast_hour=metadata.pop('forecast_hour')

    terrain = _crop_scalar(lons, lats, terrain, bbox)
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
        **metadata,
        terrain=terrain,
    )


def get_grid(cycle: str, forecast_hour: float, level: str, bbox) -> WindGrid:
    """Load one WRF cache grid by cycle and forecast hour."""
    record = _find_record(cycle, forecast_hour)
    return _record_grid(record, level, bbox)


def _record_grid(record, level, bbox):
    path = ensure_cache_file(record['path'])
    stat = path.stat()
    revision = (str(cache_dir().resolve()), stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns,
                record.get('sha256'))
    return _get_grid_cached(record['path'], level, bbox, revision)


def get_local_grid(record: dict, level: str) -> WindGrid:
    """Read an already published local file, without any network download."""
    path = cache_dir() / record['path']
    stat = path.stat()
    revision = (str(cache_dir().resolve()), stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns,
                record.get('sha256'))
    return _get_grid_cached(record['path'], level, None, revision)


def records_for_cycle(cycle: str) -> list[dict]:
    """An explicit historical batch includes archived local hours, never another cycle."""
    records = {row['valid_time']: row for row in load_index().get('files', []) if row['cycle'] == cycle}
    if native_required():
        if not records: raise ValueError('该历史批次没有原生 10 分钟 WRF 产物，不使用小时数据插值')
        return sorted(records.values(),key=lambda row:row['valid_time'])
    folder = cache_dir() / datetime.strptime(cycle, '%Y-%m-%d %H:%M UTC').strftime('%Y%m%d%H')
    for path in sorted(folder.glob('*.npz')):
        try:
            with np.load(path, allow_pickle=False) as fields:
                if str(fields['cycle_utc'].item()) != cycle:
                    continue
                metadata=npz_metadata(fields)
                valid=str(fields['valid_time_utc'].item())
                records[valid] = dict(path=str(path.relative_to(cache_dir())), cycle=cycle, **metadata,valid_time=valid)
        except (OSError, ValueError, KeyError, EOFError, BadZipFile):
            continue  # Keep usable hours; missing coverage remains explicit.
    return sorted(records.values(), key=lambda row: row['valid_time'])


def local_forecast_records(now: datetime) -> tuple[str | None, list[dict]]:
    """Select one cycle; prefer the newest with local coverage of the present."""
    schedule_index_sync()
    if not (cache_dir() / REMOTE_INDEX).is_file():
        return None, []
    records = load_index().get("files", [])
    cycles = sorted({item["cycle"] for item in records}, reverse=True)
    fallback = None
    for cycle in cycles:
        items = sorted((item for item in records if item["cycle"] == cycle), key=lambda item: item["valid_time"])
        future = any(datetime.strptime(item["valid_time"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc) > now for item in items)
        if future and fallback is None:
            fallback = (cycle, items)
        local_times = [
            datetime.strptime(item["valid_time"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            for item in items if (cache_dir() / item["path"]).is_file()
        ]
        if local_times and min(local_times) <= now < max(local_times):
            return cycle, items
    if fallback is not None:
        return fallback
    cycle = cycles[0] if cycles else None
    return cycle, [item for item in records if item["cycle"] == cycle]


def get_grid_by_valid_time(valid_time: str, level: str, bbox) -> WindGrid:
    """Load one WRF cache grid by UTC or Beijing valid-time label."""
    record = _find_record_by_valid_time(valid_time)
    return _record_grid(record, level, bbox)


def diagnostics() -> dict:
    """Return WRF cache provider diagnostics."""
    return {
        "mode": "wrf_cache",
        "cache_dir": str(cache_dir()),
        "remote_enabled": remote_configured(),
        "remote_sync": _index_sync_status(),
        "display_grid_cache": _get_grid_cached.cache_stats(),
    }
