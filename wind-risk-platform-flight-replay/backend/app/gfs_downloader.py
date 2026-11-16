"""Background GFS realtime downloader integration for the API service."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .gfs_provider import REPOSITORY_ROOT

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}

_LOCK = threading.RLock()
_WORKER: threading.Thread | None = None
_POLL_WORKER: threading.Thread | None = None
_POLL_STOP = threading.Event()
_POLL_STATE: dict[str, Any] = {"next_check_at": None, "last_check_at": None, "poll_error": None}
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

_CONNECTION_REFUSED_MARKERS = (
    "winerror 10061",
    "actively refused",
    "积极拒绝",
    "connection refused",
)
_NETWORK_HINT = (
    "[GFS 网络提示] 无法建立 GFS 下载连接（连接被拒绝）。"
    "请检查本地代理是否运行、HTTP_PROXY/HTTPS_PROXY 的地址和端口是否正确，"
    "或确认当前网络可以直连 NOAA/NOMADS；"
    "详细日志见上方 log_path。"
)


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


def check_interval_seconds() -> int:
    return max(1, _env_int("GFS_REALTIME_CHECK_INTERVAL_SECONDS", 1800))


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
        str(_env_int("GFS_REALTIME_CYCLE_COUNT", 1)),
        "--delay-hours",
        str(_env_int("GFS_REALTIME_DELAY_HOURS", 0)),
        "--retries",
        str(_env_int("GFS_REALTIME_RETRIES", 3)),
        "--timeout",
        str(_env_int("GFS_REALTIME_TIMEOUT", 180)),
        "--min-bytes",
        str(_env_int("GFS_REALTIME_MIN_BYTES", 1024)),
    ]
    if _env_bool("GFS_REALTIME_GLOBAL_REGION", False):
        command.append("--global-region")
    else:
        # Match frontend/src/api.js CHINA_BBOX: west, south, east, north.
        command.extend([
            "--subregion",
            "--leftlon", "73",
            "--bottomlat", "18",
            "--rightlon", "135",
            "--toplat", "54",
        ])
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
            **_POLL_STATE,
            "polling": bool(_POLL_WORKER and _POLL_WORKER.is_alive() and not _POLL_STOP.is_set()),
            "interval_seconds": check_interval_seconds(),
        }


def refresh_after_external_download() -> None:
    """Refresh provider-side caches after files may have changed on disk."""
    from . import gfs_provider

    gfs_provider.refresh_file_index()


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


def _is_connection_refused(line: str) -> bool:
    normalized = line.casefold()
    return any(marker in normalized for marker in _CONNECTION_REFUSED_MARKERS)


def _stream_process_output(process: subprocess.Popen[str], log_file: Any) -> tuple[bool, int]:
    """Mirror downloader output to both the backend terminal and its log file."""
    connection_refused = False
    failed_files = 0
    if process.stdout is None:
        return connection_refused, failed_files

    for line in process.stdout:
        log_file.write(line)
        log_file.flush()
        print(line, end="", flush=True)

        if not connection_refused and _is_connection_refused(line):
            connection_refused = True
            notice = _NETWORK_HINT + "\n"
            log_file.write(notice)
            log_file.flush()
            print(notice, end="", file=sys.stderr, flush=True)

        summary_match = re.search(r"\bfailed=(\d+)\b", line)
        if summary_match:
            failed_files = int(summary_match.group(1))

    return connection_refused, failed_files


def _run(command: list[str], log_path: Path) -> None:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output_dir().mkdir(parents=True, exist_ok=True)
        print(f"[GFS] 后台下载已启动，日志：{log_path}", flush=True)
        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            log_file.write("$ " + " ".join(command) + "\n")
            log_file.flush()
            process = subprocess.Popen(
                command,
                cwd=str(REPOSITORY_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            connection_refused, failed_files = _stream_process_output(process, log_file)
            return_code = process.wait()
        if return_code == 0 and failed_files == 0:
            refresh_after_external_download()
            _finish(return_code, "download completed")
        elif connection_refused:
            refresh_after_external_download()
            _finish(return_code or 1, "GFS download failed: connection refused; check configured proxy and NOAA/NOMADS connectivity")
        elif failed_files:
            refresh_after_external_download()
            _finish(return_code or 1, f"download finished with {failed_files} failed file(s); see terminal/log")
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


def _poll_realtime(stop: threading.Event, interval: float, check_on_startup: bool) -> None:
    check = check_on_startup
    try:
        while not stop.is_set():
            with _LOCK:
                _POLL_STATE["next_check_at"] = (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()
            if check and enabled():
                try:
                    with _LOCK:
                        _POLL_STATE.update(last_check_at=_utc_now_text(), poll_error=None)
                    # The existing lock rejects overlapping scheduled/manual downloads.
                    start_realtime_download("scheduled GFS update check")
                except Exception as exc:
                    with _LOCK:
                        _POLL_STATE["poll_error"] = str(exc)
            if stop.wait(interval):
                break
            check = True
    finally:
        with _LOCK:
            if _POLL_STOP is stop:
                _POLL_STATE["next_check_at"] = None


def start_polling(check_on_startup: bool = True) -> None:
    """Check NOAA every thirty minutes, independently of browser activity."""
    global _POLL_WORKER, _POLL_STOP
    with _LOCK:
        if not enabled() or (_POLL_WORKER and _POLL_WORKER.is_alive()):
            return
        _POLL_STOP = threading.Event()
        _POLL_WORKER = threading.Thread(target=_poll_realtime,
            args=(_POLL_STOP, check_interval_seconds(), check_on_startup), name="gfs-update-poll", daemon=True)
        _POLL_WORKER.start()


def stop_polling() -> None:
    """Stop scheduling; a running downloader keeps its existing lifecycle."""
    with _LOCK:
        _POLL_STOP.set()
        worker = _POLL_WORKER
        _POLL_STATE["next_check_at"] = None
    if worker and worker is not threading.current_thread():
        worker.join(timeout=2)
