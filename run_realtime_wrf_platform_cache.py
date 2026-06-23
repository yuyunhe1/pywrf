#!/usr/bin/env python3
"""Download the latest usable GFS cycle, run 24 h WRF, and export map cache.

The platform cache is intentionally simple: one compressed NPZ file per WRF
valid time, plus an index.json. The web backend can read the directory locally
or mirror it from a server through SFTP.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np


BEIJING_TZ = timezone(timedelta(hours=8))
NOMADS_FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl"
DEFAULT_WPS_VARS = ("APCP", "HGT", "PRMSL", "SPFH", "TMP", "UGRD", "VGRD")
DEFAULT_HEIGHTS_M = (10, 30, 50, 80, 100, 200, 300, 500, 800, 1000, 1500, 2000, 3000)
GRAVITY = 9.81


@dataclass(frozen=True)
class CacheRecord:
    cycle: datetime
    forecast_hour: int
    valid_time: datetime
    relative_path: str
    bbox: list[float]


def floor_to_gfs_cycle(value: datetime) -> datetime:
    hour = (value.hour // 6) * 6
    return value.replace(hour=hour, minute=0, second=0, microsecond=0)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_bj(value: datetime) -> str:
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M 北京时间")


def build_gfs_url(cycle: datetime, forecast_hour: int, variables: tuple[str, ...] | None = None) -> str:
    ymd = cycle.strftime("%Y%m%d")
    hh = cycle.strftime("%H")
    params = {
        "dir": f"/gfs.{ymd}/{hh}/atmos",
        "file": f"gfs.t{hh}z.pgrb2.0p25.f{forecast_hour:03d}",
    }
    if variables:
        for variable in variables:
            params[f"var_{variable.upper()}"] = "on"
    else:
        params["all_var"] = "on"
    params["all_lev"] = "on"
    return f"{NOMADS_FILTER_URL}?{urllib.parse.urlencode(params)}"


def gdex_style_path(gfs_dir: Path, cycle: datetime, forecast_hour: int) -> Path:
    ymd = cycle.strftime("%Y%m%d")
    ymdh = cycle.strftime("%Y%m%d%H")
    return gfs_dir / ymd / f"gfs.0p25.{ymdh}.f{forecast_hour:03d}.grib2"


def is_valid_file(path: Path, min_bytes: int) -> bool:
    return path.exists() and path.stat().st_size >= min_bytes


def download(url: str, target: Path, retries: int, timeout: int, min_bytes: int) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    for attempt in range(1, retries + 1):
        try:
            print(f"[DOWNLOAD] {target.name}")
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise RuntimeError(f"HTTP status={getattr(response, 'status', None)}")
                with tmp.open("wb") as file:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        file.write(chunk)
            if tmp.stat().st_size < min_bytes:
                snippet = tmp.read_bytes()[:300].decode("utf-8", errors="replace")
                raise RuntimeError(f"file too small, probably unavailable: {snippet}")
            tmp.replace(target)
            print(f"[DONE] {target} ({target.stat().st_size / 1024 / 1024:.1f} MiB)")
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError, TimeoutError) as exc:
            print(f"[WARN] {target.name} attempt {attempt}/{retries}: {exc}", file=sys.stderr)
            if tmp.exists():
                tmp.unlink()
            if attempt < retries:
                time.sleep(10 * attempt)
    return False


def probe_cycle(cycle: datetime, args) -> bool:
    target = gdex_style_path(args.gfs_dir, cycle, 1)
    if is_valid_file(target, args.min_bytes):
        return True
    return download(build_gfs_url(cycle, 1, args.gfs_vars), target, args.retries, args.timeout, args.min_bytes)


def choose_cycle(args) -> datetime:
    anchor = args.now or datetime.now(timezone.utc) - timedelta(hours=args.delay_hours)
    latest = floor_to_gfs_cycle(anchor)
    for index in range(args.cycle_fallback_count):
        cycle = latest - timedelta(hours=6 * index)
        print(f"[PROBE] cycle={cycle:%Y%m%d%H} f001")
        if probe_cycle(cycle, args):
            print(f"[CYCLE] selected {cycle:%Y-%m-%d_%H:%M:%S} UTC")
            return cycle
        print(f"[CYCLE] {cycle:%Y%m%d%H} f001 unavailable, try previous cycle")
    raise SystemExit("No usable GFS cycle found. Increase --cycle-fallback-count or wait for NOMADS publication.")


def download_wrf_forcing(cycle: datetime, args) -> None:
    # WRF/WPS in this repository is configured for 3-hour forcing. The platform
    # exports f001-f024 from the completed hourly WRF output.
    required_hours = list(range(0, args.forecast_hours + 1, args.gfs_interval_hours))
    for hour in required_hours:
        target = gdex_style_path(args.gfs_dir, cycle, hour)
        if is_valid_file(target, args.min_bytes):
            print(f"[SKIP] {target.name}")
            continue
        ok = download(build_gfs_url(cycle, hour, args.gfs_vars), target, args.retries, args.timeout, args.min_bytes)
        if not ok:
            raise SystemExit(f"Failed to download required WRF forcing f{hour:03d} for cycle {cycle:%Y%m%d%H}")


def run_wrf(cycle: datetime, args) -> None:
    env = os.environ.copy()
    env["WRF_START_DATE"] = cycle.strftime("%Y-%m-%d_%H:%M:%S")
    env["GFS_CYCLE_TIME"] = cycle.strftime("%Y-%m-%d_%H:%M:%S")
    env["WRF_FORECAST_HOURS"] = str(args.forecast_hours)
    env["WRF_NUM_PROC"] = str(args.num_proc)
    env["REUSE_GEOGRID"] = "1" if args.reuse_geogrid else "0"
    command = [sys.executable, "main.py"]
    print("[WRF] " + " ".join(command))
    subprocess.run(command, cwd=args.base_dir, env=env, check=True)


def open_wrf_dataset(path: Path):
    try:
        from netCDF4 import Dataset
    except ImportError as exc:
        raise SystemExit("Exporting WRF cache requires netCDF4: conda install -c conda-forge netcdf4") from exc
    return Dataset(path, "r")


def as_array(values) -> np.ndarray:
    return np.asarray(np.ma.filled(values, np.nan), dtype=float)


def decode_time_row(row) -> str:
    values = np.asarray(row)
    if values.dtype.kind == "S":
        return b"".join(values.tolist()).decode("ascii", errors="replace").strip()
    return "".join(values.astype(str).tolist()).strip()


def read_times(dataset) -> list[datetime]:
    raw = np.asarray(dataset.variables["Times"][:])
    if raw.ndim == 1:
        raw = raw[np.newaxis, :]
    return [
        datetime.strptime(decode_time_row(row), "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)
        for row in raw
    ]


def read_time_slice(variable, time_index: int) -> np.ndarray:
    if variable.ndim >= 3 and variable.dimensions[0] == "Time":
        return as_array(variable[time_index])
    return as_array(variable[:])


def rotate_to_earth(u: np.ndarray, v: np.ndarray, cosalpha: np.ndarray, sinalpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return u * cosalpha - v * sinalpha, v * cosalpha + u * sinalpha


def interpolate_levels(heights_agl: np.ndarray, u: np.ndarray, v: np.ndarray, u10: np.ndarray | None, v10: np.ndarray | None, target_heights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    nz, ny, nx = heights_agl.shape
    out_u = np.full((len(target_heights), ny, nx), np.nan, dtype=np.float32)
    out_v = np.full((len(target_heights), ny, nx), np.nan, dtype=np.float32)

    for level_index, height in enumerate(target_heights):
        if math.isclose(float(height), 10.0) and u10 is not None and v10 is not None:
            out_u[level_index] = u10.astype(np.float32)
            out_v[level_index] = v10.astype(np.float32)
            continue

        above = heights_agl >= height
        has_above = above.any(axis=0)
        upper = above.argmax(axis=0)
        lower = np.maximum(upper - 1, 0)
        valid = has_above & (upper > 0)

        upper3 = upper[np.newaxis, :, :]
        lower3 = lower[np.newaxis, :, :]
        z0 = np.take_along_axis(heights_agl, lower3, axis=0)[0]
        z1 = np.take_along_axis(heights_agl, upper3, axis=0)[0]
        u0 = np.take_along_axis(u, lower3, axis=0)[0]
        u1 = np.take_along_axis(u, upper3, axis=0)[0]
        v0 = np.take_along_axis(v, lower3, axis=0)[0]
        v1 = np.take_along_axis(v, upper3, axis=0)[0]
        weight = np.divide(height - z0, z1 - z0, out=np.full_like(z0, np.nan), where=np.abs(z1 - z0) > 1e-6)
        out_u[level_index] = np.where(valid, u0 + weight * (u1 - u0), np.nan).astype(np.float32)
        out_v[level_index] = np.where(valid, v0 + weight * (v1 - v0), np.nan).astype(np.float32)
    return out_u, out_v


def regular_grid_mapper(xlat: np.ndarray, xlon: np.ndarray, spacing_deg: float | None):
    source_points = np.column_stack([xlon.ravel(), xlat.ravel()])
    if spacing_deg is None:
        dlon = np.nanmedian(np.abs(np.diff(xlon, axis=1)))
        dlat = np.nanmedian(np.abs(np.diff(xlat, axis=0)))
        spacing_deg = float(np.nanmin([dlon, dlat]))
    spacing_deg = max(spacing_deg, 1e-4)
    lons = np.arange(float(np.nanmin(xlon)), float(np.nanmax(xlon)) + spacing_deg * 0.5, spacing_deg)
    lats = np.arange(float(np.nanmax(xlat)), float(np.nanmin(xlat)) - spacing_deg * 0.5, -spacing_deg)
    target_lon, target_lat = np.meshgrid(lons, lats)
    target_points = np.column_stack([target_lon.ravel(), target_lat.ravel()])

    try:
        from scipy.spatial import cKDTree

        _, indices = cKDTree(source_points).query(target_points, k=1)
    except ImportError:
        indices = []
        chunk = 5000
        for start in range(0, len(target_points), chunk):
            diff = source_points[np.newaxis, :, :] - target_points[start : start + chunk, np.newaxis, :]
            indices.extend(np.argmin(np.sum(diff * diff, axis=2), axis=1).tolist())
        indices = np.asarray(indices, dtype=int)
    return lons.astype(np.float32), lats.astype(np.float32), np.asarray(indices, dtype=np.int64)


def apply_mapper(values: np.ndarray, target_shape: tuple[int, int], indices: np.ndarray) -> np.ndarray:
    nz = values.shape[0]
    flattened = values.reshape(nz, -1)
    return flattened[:, indices].reshape((nz, *target_shape)).astype(np.float32)


def export_wrf_file(path: Path, cycle: datetime, args) -> list[CacheRecord]:
    records: list[CacheRecord] = []
    heights = np.asarray(args.heights, dtype=np.float32)
    with open_wrf_dataset(path) as dataset:
        times = read_times(dataset)
        xlat = read_time_slice(dataset.variables["XLAT"], 0)
        xlon = read_time_slice(dataset.variables["XLONG"], 0)
        lons, lats, mapper = regular_grid_mapper(xlat, xlon, args.output_spacing_deg)
        target_shape = (len(lats), len(lons))
        cosalpha = read_time_slice(dataset.variables["COSALPHA"], 0) if "COSALPHA" in dataset.variables else np.ones_like(xlat)
        sinalpha = read_time_slice(dataset.variables["SINALPHA"], 0) if "SINALPHA" in dataset.variables else np.zeros_like(xlat)

        for time_index, valid_time in enumerate(times):
            forecast_hour = int(round((valid_time - cycle).total_seconds() / 3600))
            if forecast_hour < args.export_start_fhour or forecast_hour > args.forecast_hours:
                continue
            print(f"[EXPORT] {path.name} time={valid_time:%Y-%m-%d_%H:%M:%S} f{forecast_hour:03d}")
            u_stag = as_array(dataset.variables["U"][time_index])
            v_stag = as_array(dataset.variables["V"][time_index])
            grid_u = 0.5 * (u_stag[:, :, :-1] + u_stag[:, :, 1:])
            grid_v = 0.5 * (v_stag[:, :-1, :] + v_stag[:, 1:, :])
            earth_u, earth_v = rotate_to_earth(grid_u, grid_v, cosalpha[np.newaxis, :, :], sinalpha[np.newaxis, :, :])

            ph = as_array(dataset.variables["PH"][time_index])
            phb = as_array(dataset.variables["PHB"][time_index])
            hgt = read_time_slice(dataset.variables["HGT"], time_index)
            z_interfaces = (ph + phb) / GRAVITY
            heights_agl = 0.5 * (z_interfaces[:-1] + z_interfaces[1:]) - hgt[np.newaxis, :, :]

            u10 = v10 = None
            if "U10" in dataset.variables and "V10" in dataset.variables:
                u10_grid = read_time_slice(dataset.variables["U10"], time_index)
                v10_grid = read_time_slice(dataset.variables["V10"], time_index)
                u10, v10 = rotate_to_earth(u10_grid, v10_grid, cosalpha, sinalpha)

            level_u, level_v = interpolate_levels(heights_agl, earth_u, earth_v, u10, v10, heights)
            regular_u = apply_mapper(level_u, target_shape, mapper)
            regular_v = apply_mapper(level_v, target_shape, mapper)

            rel = Path(cycle.strftime("%Y%m%d%H")) / f"wrf_d{args.domain:02d}_{cycle:%Y%m%d%H}_f{forecast_hour:03d}.npz"
            out_path = args.cache_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                out_path,
                lons=lons,
                lats=lats,
                levels_m=heights,
                u=regular_u,
                v=regular_v,
                cycle_utc=np.asarray(cycle.strftime("%Y-%m-%d %H:%M UTC")),
                valid_time_utc=np.asarray(valid_time.strftime("%Y-%m-%d %H:%M UTC")),
                forecast_hour=np.asarray(forecast_hour, dtype=np.int16),
            )
            records.append(
                CacheRecord(
                    cycle=cycle,
                    forecast_hour=forecast_hour,
                    valid_time=valid_time,
                    relative_path=rel.as_posix(),
                    bbox=[float(lons[0]), float(lats[-1]), float(lons[-1]), float(lats[0])],
                )
            )
    return records


def find_wrf_outputs(cycle: datetime, args) -> list[Path]:
    output_day = args.wrfout_dir / cycle.strftime("%Y-%m-%d")
    files = sorted(output_day.glob(f"wrfout_d{args.domain:02d}_*"))
    if not files:
        raise SystemExit(f"No wrfout_d{args.domain:02d}_* found under {output_day}")
    return files


def write_index(records: list[CacheRecord], args) -> None:
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    by_valid: dict[str, CacheRecord] = {}
    for record in records:
        by_valid[format_utc(record.valid_time)] = record
    selected = [by_valid[key] for key in sorted(by_valid)]
    levels = [f"{int(height)}m AGL" for height in args.heights]
    payload = {
        "source": "WRF d02 platform cache",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "levels": levels,
        "cycles": sorted({format_utc(item.cycle) for item in selected}),
        "forecast_hours": sorted({item.forecast_hour for item in selected}),
        "forecast_hours_by_cycle": {
            cycle: sorted({item.forecast_hour for item in selected if format_utc(item.cycle) == cycle})
            for cycle in sorted({format_utc(item.cycle) for item in selected})
        },
        "valid_times": [
            {
                "label": format_bj(item.valid_time),
                "valid_time": format_utc(item.valid_time),
                "cycle": format_utc(item.cycle),
                "cycle_bj": format_bj(item.cycle),
                "forecast_hour": item.forecast_hour,
            }
            for item in selected
        ],
        "files": [
            {
                "cycle": format_utc(item.cycle),
                "cycle_bj": format_bj(item.cycle),
                "forecast_hour": item.forecast_hour,
                "valid_time": format_utc(item.valid_time),
                "valid_time_bj": format_bj(item.valid_time),
                "path": item.relative_path,
                "bbox": item.bbox,
            }
            for item in selected
        ],
    }
    (args.cache_dir / "index.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[CACHE] index written: {args.cache_dir / 'index.json'}")


def export_cache(cycle: datetime, args) -> None:
    records: list[CacheRecord] = []
    for path in find_wrf_outputs(cycle, args):
        records.extend(export_wrf_file(path, cycle, args))
    if not records:
        raise SystemExit("No WRF time slices were exported. Check WRF history_interval and output files.")
    write_index(records, args)


def parse_args():
    parser = argparse.ArgumentParser(description="Run realtime GFS -> WRF -> platform cache workflow.")
    parser.add_argument("--base-dir", type=Path, default=Path("/root/pyWRF-automation"))
    parser.add_argument("--gfs-dir", type=Path, default=Path("/root/pyWRF-automation/data/gdex_gfs_0p25_global"))
    parser.add_argument("--wrfout-dir", type=Path, default=Path("/root/pyWRF-automation/wrf_output"))
    parser.add_argument("--cache-dir", type=Path, default=Path("/root/pyWRF-automation/data/wrf_platform_cache"))
    parser.add_argument("--forecast-hours", type=int, default=24)
    parser.add_argument("--export-start-fhour", type=int, default=1)
    parser.add_argument("--gfs-interval-hours", type=int, default=3)
    parser.add_argument(
        "--gfs-vars",
        type=lambda text: tuple(item.strip().upper() for item in text.split(",") if item.strip()),
        default=None,
        help="Optional NOMADS variable subset, comma-separated. Default downloads all variables.",
    )
    parser.add_argument("--cycle-fallback-count", type=int, default=4)
    parser.add_argument("--delay-hours", type=int, default=0)
    parser.add_argument("--now", type=lambda text: datetime.strptime(text, "%Y%m%d%H").replace(tzinfo=timezone.utc), default=None)
    parser.add_argument("--num-proc", type=int, default=4)
    parser.add_argument("--domain", type=int, default=2)
    parser.add_argument("--reuse-geogrid", action="store_true", default=True)
    parser.add_argument("--no-reuse-geogrid", dest="reuse_geogrid", action="store_false")
    parser.add_argument("--heights", type=lambda text: tuple(int(item) for item in text.split(",")), default=DEFAULT_HEIGHTS_M)
    parser.add_argument("--output-spacing-deg", type=float, default=None)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--min-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--wrf-only", action="store_true")
    parser.add_argument("--export-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cycle = choose_cycle(args)
    if not args.export_only:
        download_wrf_forcing(cycle, args)
    if args.download_only:
        return
    if not args.export_only:
        run_wrf(cycle, args)
    if not args.wrf_only:
        export_cache(cycle, args)


if __name__ == "__main__":
    main()
