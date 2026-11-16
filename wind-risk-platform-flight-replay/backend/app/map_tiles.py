"""Same-origin OpenStreetMap tile proxy with a persistent local cache."""

from __future__ import annotations

import os
import threading
import urllib.error
import urllib.request
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TILE_CACHE_DIR = Path(
    os.getenv("MAP_TILE_CACHE_DIR", str(REPOSITORY_ROOT / "data" / "map_tile_cache"))
).expanduser()
TILE_SOURCE_URL = os.getenv(
    "MAP_TILE_SOURCE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
)
TILE_USER_AGENT = os.getenv(
    "MAP_TILE_USER_AGENT",
    "wind-risk-platform/0.1 (temporary academic demonstration)",
)
TILE_TIMEOUT_SECONDS = float(os.getenv("MAP_TILE_TIMEOUT_SECONDS", "20"))
MAX_TILE_ZOOM = int(os.getenv("MAP_TILE_MAX_ZOOM", "18"))
MAX_TILE_BYTES = int(os.getenv("MAP_TILE_MAX_BYTES", str(2 * 1024 * 1024)))

_LOCKS_GUARD = threading.RLock()
_TILE_LOCKS: dict[tuple[int, int, int], threading.Lock] = {}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class TileCoordinateError(ValueError):
    """Raised when a slippy-map tile coordinate is outside the valid range."""


class TileDownloadError(RuntimeError):
    """Raised when an upstream tile cannot be retrieved safely."""


def validate_tile_coordinate(z: int, x: int, y: int) -> None:
    if z < 0 or z > MAX_TILE_ZOOM:
        raise TileCoordinateError(f"地图缩放层级必须位于 0-{MAX_TILE_ZOOM}")
    side = 1 << z
    if x < 0 or x >= side or y < 0 or y >= side:
        raise TileCoordinateError("地图瓦片坐标超出当前缩放层级范围")


def cache_path(z: int, x: int, y: int) -> Path:
    validate_tile_coordinate(z, x, y)
    return TILE_CACHE_DIR / str(z) / str(x) / f"{y}.png"


def _download_tile(z: int, x: int, y: int) -> bytes:
    url = TILE_SOURCE_URL.format(z=z, x=x, y=y)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": TILE_USER_AGENT,
            "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TILE_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type()
            payload = response.read(MAX_TILE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TileDownloadError(
            "底图服务器连接失败；请确认后端代理可用后重试"
        ) from exc

    if len(payload) > MAX_TILE_BYTES:
        raise TileDownloadError("底图瓦片响应异常：文件过大")
    if content_type not in {"image/png", "application/octet-stream"}:
        raise TileDownloadError(f"底图瓦片响应类型异常：{content_type}")
    if not payload.startswith(_PNG_SIGNATURE):
        raise TileDownloadError("底图瓦片响应不是有效 PNG 图片")
    return payload


def _tile_lock(z: int, x: int, y: int) -> threading.Lock:
    key = (z, x, y)
    with _LOCKS_GUARD:
        return _TILE_LOCKS.setdefault(key, threading.Lock())


def get_cached_tile(z: int, x: int, y: int) -> tuple[Path, bool]:
    """Return a cached tile path and whether it was downloaded in this call."""
    target = cache_path(z, x, y)
    if target.is_file() and target.stat().st_size > len(_PNG_SIGNATURE):
        return target, False

    # Leaflet may request the same missing tile concurrently. Serialize cache misses
    # so the public upstream receives only one request for each local tile.
    with _tile_lock(z, x, y):
        if target.is_file() and target.stat().st_size > len(_PNG_SIGNATURE):
            return target, False
        payload = _download_tile(z, x, y)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".png.tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)
        return target, True
