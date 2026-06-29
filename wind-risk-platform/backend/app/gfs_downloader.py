"""Background GFS realtime downloader integration for the API service."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .gfs_provider import REPOSITORY_ROOT

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}

_LOCK = threading.RLock()
_WORKER: threading.Thread | None = None
_STATE: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "return_code": None,
    "message": "not started",
    "reason": None,
    "command": None,
    "log_path": None,
}


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def enabled() -> bool:
    return _env_bool("GFS_AUTO_DOWNLOAD", True)


def output_dir() -> Path:
    return Path(os.getenv("GFS_REALTIME_DOWNLOAD_DIR", str(REPOSITORY_ROOT / "data" / "gfs_hourly_windcheck"))).expanduser()


def log_dir() -> Path:
    return Path(os.getenv("GFS_REALTIME_LOG_DIR", str(REPOSITORY_ROOT / "data" / "gfs_download_logs"))).expanduser()


def downloader_script() -> Path:
    return Path(
        os.getenv(
            "GFS_DOWNLOADER_SCRIPT",
            str(REPOSITORY_ROOT / "download_gfs_hourly_70vars_realtime.py"),
        )
    ).expanduser()


def _build_command() -> list[str]:
    command = [
        os.getenv("GFS_DOWNLOADER_PYTHON", sys.executable),
        str(downloader_script()),
        "realtime",
        "--output-dir",
        str(output_dir()),
        "--start-fhour",
        str(_env_int("GFS_REALTIME_START_FHOUR", 1)),
        "--end-fhour",
        str(_env_int("GFS_REALTIME_END_FHOUR", 12)),
        "--cycle-count",
        str(_env_int("GFS_REALTIME_CYCLE_COUNT", 2)),
        "--delay-hours",
        str(_env_int("GFS_REALTIME_DELAY_HOURS", 0)),
        "--retries",
        str(_env_int("GFS_REALTIME_RETRIES", 3)),
        "--timeout",
        str(_env_int("GFS_REALTIME_TIMEOUT", 180)),
        "--min-bytes",
        str(_env_int("GFS_REALTIME_MIN_BYTES", 1024)),
    ]
    if _env_bool("GFS_REALTIME_GLOBAL_REGION", True):
        command.append("--global-region")
    if _env_bool("GFS_REALTIME_WIND_MAP_ONLY", False):
        command.append("--wind-map-only")
    if not _env_bool("GFS_REALTIME_FALLBACK_PREVIOUS_CYCLE", True):
        command.append("--no-fallback-previous-cycle")
    if not _env_bool("GFS_REALTIME_PROBE_F001", True):
        command.append("--no-probe-f001")
    debug_now = os.getenv("GFS_REALTIME_NOW")
    if debug_now:
        command.extend(["--now", debug_now])
    return command


def status() -> dict[str, Any]:
    with _LOCK:
        return {
            "enabled": enabled(),
            "output_dir": str(output_dir()),
            "script": str(downloader_script()),
            **_STATE,
        }


def _finish(return_code: int | None, message: str) -> None:
    with _LOCK:
        _STATE.update(
            {
                "running": False,
                "finished_at": _utc_now_text(),
                "return_code": return_code,
                "message": message,
            }
        )


def _run(command: list[str], log_path: Path) -> None:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output_dir().mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            log_file.write("$ " + " ".join(command) + "\n")
            log_file.flush()
            process = subprocess.Popen(
                command,
                cwd=str(REPOSITORY_ROOT),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            return_code = process.wait()
        if return_code == 0:
            from . import gfs_provider

            gfs_provider.refresh_file_index()
            _finish(return_code, "download completed")
        else:
            _finish(return_code, f"download failed with exit code {return_code}")
    except Exception as exc:  # pragma: no cover - defensive status reporting
        _finish(None, f"download failed before completion: {exc}")


def start_realtime_download(reason: str = "api request", force: bool = False) -> dict[str, Any]:
    """Start one realtime downloader process in the background if none is running."""
    global _WORKER
    with _LOCK:
        if _STATE["running"]:
            return status()
        if not enabled() and not force:
            _STATE.update({"message": "auto download disabled", "reason": reason})
            return status()

        command = _build_command()
        log_path = log_dir() / f"gfs_realtime_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log"
        _STATE.update(
            {
                "running": True,
                "started_at": _utc_now_text(),
                "finished_at": None,
                "return_code": None,
                "message": "download running",
                "reason": reason,
                "command": command,
                "log_path": str(log_path),
            }
        )
        _WORKER = threading.Thread(target=_run, args=(command, log_path), daemon=True)
        _WORKER.start()
        return status()
