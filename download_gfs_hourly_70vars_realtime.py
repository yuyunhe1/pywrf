import argparse
import json
import math
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pandas as pd


warnings.filterwarnings("ignore", message="Workbook contains no default style")

NOMADS_FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl"
GDEX_BASE_URL = "https://singapore.nationalresearchplatform.org:8443/ncar/gdex/d084001"
BEIJING_TZ = timezone(timedelta(hours=8))

# HGT on isobaric levels is the key variable for mapping pressure levels to
# geometric height. HGT:surface is used to convert MSL height to AGL height.
WIND_CHECK_VARS = ["HGT", "PRES", "UGRD", "VGRD"]
WIND_CHECK_PRESSURE_LEVELS_HPA = [
    1000,
    975,
    950,
    925,
    900,
    850,
    800,
    750,
    700,
    650,
    600,
    550,
    500,
    450,
    400,
    350,
    300,
    250,
    200,
    150,
    100,
    50,
]
WIND_CHECK_LEVELS = (
    [
        "surface",
        "10_m_above_ground",
        "20_m_above_ground",
        "30_m_above_ground",
        "40_m_above_ground",
        "50_m_above_ground",
        "80_m_above_ground",
        "100_m_above_ground",
        "30-0_mb_above_ground",
        "max_wind",
    ]
    + [f"{level}_mb" for level in WIND_CHECK_PRESSURE_LEVELS_HPA]
)
ABOVE_GROUND_WGRIB2_LEVELS = [
    "10 m above ground",
    "20 m above ground",
    "30 m above ground",
    "40 m above ground",
    "50 m above ground",
    "80 m above ground",
    "100 m above ground",
    "30-0 mb above ground",
    "max wind",
]
WIND_MAP_VARS = ["UGRD", "VGRD"]
WIND_MAP_LEVELS = [
    "10_m_above_ground",
    "20_m_above_ground",
    "30_m_above_ground",
    "40_m_above_ground",
    "50_m_above_ground",
    "80_m_above_ground",
    "100_m_above_ground",
]


def requested_variables_and_levels(args):
    """Return the NOMADS selection for validation or lightweight map display."""
    if getattr(args, "wind_map_only", False):
        return WIND_MAP_VARS, WIND_MAP_LEVELS
    return WIND_CHECK_VARS, WIND_CHECK_LEVELS


def floor_to_gfs_cycle(dt):
    cycle_hour = (dt.hour // 6) * 6
    return dt.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


def parse_utc_hour(value):
    return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def parse_month_start(value):
    return datetime.strptime(value, "%Y%m").replace(tzinfo=timezone.utc)


def month_end(value):
    start = parse_month_start(value)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    return nxt - timedelta(hours=1)


def parse_now(args):
    if args.now:
        return parse_utc_hour(args.now)
    return datetime.now(timezone.utc) - timedelta(hours=args.delay_hours)


def build_cycles(anchor_dt, cycle_count):
    latest_cycle = floor_to_gfs_cycle(anchor_dt)
    return [latest_cycle - timedelta(hours=6 * i) for i in range(cycle_count)]


def build_cycles_between(start_dt, end_dt):
    cur = floor_to_gfs_cycle(start_dt)
    end = floor_to_gfs_cycle(end_dt)
    while cur <= end:
        yield cur
        cur += timedelta(hours=6)


def add_filter_params(params, variables, levels):
    for var in variables:
        params[f"var_{var}"] = "on"
    for level in levels:
        params[f"lev_{level}"] = "on"


def build_nomads_url(
    cycle_dt,
    forecast_hour,
    global_region=True,
    top_lat=90,
    bottom_lat=-90,
    left_lon=0,
    right_lon=360,
    variables=None,
    levels=None,
):
    ymd = cycle_dt.strftime("%Y%m%d")
    hh = cycle_dt.strftime("%H")
    params = {
        "dir": f"/gfs.{ymd}/{hh}/atmos",
        "file": f"gfs.t{hh}z.pgrb2.0p25.f{forecast_hour:03d}",
    }
    add_filter_params(params, variables or WIND_CHECK_VARS, levels or WIND_CHECK_LEVELS)

    if not global_region:
        params.update(
            {
                "subregion": "",
                "toplat": str(top_lat),
                "leftlon": str(left_lon),
                "rightlon": str(right_lon),
                "bottomlat": str(bottom_lat),
            }
        )

    return f"{NOMADS_FILTER_URL}?{urllib.parse.urlencode(params)}"


def output_path(output_dir, cycle_dt, forecast_hour, global_region=False):
    valid_dt = cycle_dt + timedelta(hours=forecast_hour)
    valid_prefix = valid_dt.strftime("%m%d")
    ymd = cycle_dt.strftime("%Y%m%d")
    hh = cycle_dt.strftime("%H")
    region = "global" if global_region else "point"
    name = f"{valid_prefix}_gfs_{ymd}_{hh}z_f{forecast_hour:03d}_{region}_windcheck.grib2"
    return Path(output_dir) / name


def meta_path(path):
    path = Path(path)
    return path.with_suffix(path.suffix + ".meta.json")


def read_meta(path):
    path = meta_path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_meta(path, meta):
    meta_path(path).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def is_valid_file(path, min_bytes):
    path = Path(path)
    return path.exists() and path.stat().st_size >= min_bytes


def download(url, target, retries=3, timeout=180, min_bytes=1024):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")

    for attempt in range(1, retries + 1):
        try:
            print(f"[DOWNLOAD] {target.name}")
            print(f"  {url}")
            with urllib.request.urlopen(url, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise RuntimeError(f"HTTP status={status}")

                with tmp.open("wb") as f:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)

            if tmp.stat().st_size < min_bytes:
                text = tmp.read_bytes()[:500].decode("utf-8", errors="ignore")
                raise RuntimeError(f"下载文件过小，可能该时效尚未发布。前 500 字节: {text}")

            tmp.replace(target)
            print(f"[DONE] {target} ({target.stat().st_size / 1024:.1f} KiB)")
            return True
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            RuntimeError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
        ) as exc:
            print(f"[ERROR] {target.name} attempt {attempt}/{retries}: {exc}", file=sys.stderr)
            if tmp.exists():
                tmp.unlink()
            if attempt < retries:
                time.sleep(10 * attempt)

    return False


def make_source_plan(target_cycle_dt, target_forecast_hour, use_fallback=True, source_mode="auto"):
    preferred = {
        "kind": "preferred",
        "source_cycle_dt": target_cycle_dt,
        "source_forecast_hour": target_forecast_hour,
        "target_cycle_dt": target_cycle_dt,
        "target_forecast_hour": target_forecast_hour,
    }
    if source_mode == "preferred" or not use_fallback:
        return [preferred]

    fallback = {
        "kind": "fallback_previous_cycle",
        "source_cycle_dt": target_cycle_dt - timedelta(hours=6),
        "source_forecast_hour": target_forecast_hour + 6,
        "target_cycle_dt": target_cycle_dt,
        "target_forecast_hour": target_forecast_hour,
    }
    if source_mode == "fallback":
        return [fallback]

    return [preferred, fallback]


def format_plan_item(item):
    src_cycle = item["source_cycle_dt"].strftime("%Y%m%d%H")
    tgt_cycle = item["target_cycle_dt"].strftime("%Y%m%d%H")
    return (
        f"{item['kind']}: source={src_cycle} f{item['source_forecast_hour']:03d} "
        f"-> target={tgt_cycle} f{item['target_forecast_hour']:03d}"
    )


def sync_target(args, target_cycle_dt, target_forecast_hour, source_mode="auto"):
    target = output_path(args.output_dir, target_cycle_dt, target_forecast_hour, args.global_region)
    existing_meta = read_meta(target)
    existing_kind = existing_meta.get("kind")

    if is_valid_file(target, args.min_bytes) and existing_kind == "preferred" and source_mode != "fallback":
        print(f"[SKIP] {target.name} (preferred already exists)")
        return "skipped_preferred"

    if is_valid_file(target, args.min_bytes) and existing_kind == "fallback_previous_cycle" and source_mode == "fallback":
        print(f"[SKIP] {target.name} (fallback already exists)")
        return "skipped_fallback"

    if is_valid_file(target, args.min_bytes) and existing_kind == "fallback_previous_cycle":
        print(f"[CHECK] {target.name} 当前是 fallback，尝试用最新起报点正式数据覆盖")

    for item in make_source_plan(target_cycle_dt, target_forecast_hour, args.fallback_previous_cycle, source_mode):
        if item["kind"] == "fallback_previous_cycle" and is_valid_file(target, args.min_bytes):
            print(f"[SKIP] {target.name} 最新正式数据暂不可用，已有 fallback 文件")
            return "skipped_fallback"

        source_cycle_dt = item["source_cycle_dt"]
        source_forecast_hour = item["source_forecast_hour"]
        variables, levels = requested_variables_and_levels(args)
        url = build_nomads_url(
            cycle_dt=source_cycle_dt,
            forecast_hour=source_forecast_hour,
            global_region=args.global_region,
            top_lat=args.toplat,
            bottom_lat=args.bottomlat,
            left_lon=args.leftlon,
            right_lon=args.rightlon,
            variables=variables,
            levels=levels,
        )
        print(f"[PLAN] {format_plan_item(item)}")

        if args.print_url_only:
            print(f"[TARGET] {target}")
            print(url)
            continue

        ok = download(url, target, retries=args.retries, timeout=args.timeout, min_bytes=args.min_bytes)
        if ok:
            write_grib_meta(target, item, args)
            return "downloaded_preferred" if item["kind"] == "preferred" else "downloaded_fallback"

    return "printed" if args.print_url_only else "failed"


def write_grib_meta(target, item, args):
    source_cycle_dt = item["source_cycle_dt"]
    source_forecast_hour = item["source_forecast_hour"]
    variables, levels = requested_variables_and_levels(args)
    bounds = {
        "toplat": 90.0,
        "bottomlat": -90.0,
        "leftlon": -180.0,
        "rightlon": 180.0,
    } if args.global_region else {
        "toplat": args.toplat,
        "bottomlat": args.bottomlat,
        "leftlon": args.leftlon,
        "rightlon": args.rightlon,
    }
    write_meta(
        target,
        {
            "kind": item["kind"],
            "source_cycle_utc": source_cycle_dt.strftime("%Y%m%d%H"),
            "source_forecast_hour": source_forecast_hour,
            "target_cycle_utc": item["target_cycle_dt"].strftime("%Y%m%d%H"),
            "target_forecast_hour": item["target_forecast_hour"],
            "valid_time_utc": (source_cycle_dt + timedelta(hours=source_forecast_hour)).strftime("%Y%m%d%H"),
            "lat": args.lat,
            "lon": args.lon,
            **bounds,
            "variables": variables,
            "levels": levels,
            "wind_map_only": getattr(args, "wind_map_only", False),
            "height_mapping": "pressure levels use HGT:isobaricInhPa; AGL height = HGT:isobaricInhPa - HGT:surface",
            "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )


def update_counts(result, counts):
    if result == "downloaded_preferred":
        counts["downloaded"] += 1
    elif result == "downloaded_fallback":
        counts["fallback"] += 1
    elif result in {"skipped_preferred", "skipped_fallback", "printed"}:
        counts["skipped"] += 1
    else:
        counts["failed"] += 1


def sync_once(args):
    anchor_dt = parse_now(args)
    cycles = build_cycles(anchor_dt, args.cycle_count)
    forecast_hours = list(range(args.start_fhour, args.end_fhour + 1))

    print("=" * 100)
    print(f"Anchor UTC: {anchor_dt:%Y-%m-%d %H:%M:%S}")
    print(f"Cycles UTC: {[c.strftime('%Y%m%d%H') for c in cycles]}")
    print(f"Forecast hours: {forecast_hours}")
    print(f"Output dir: {args.output_dir}")

    counts = {"downloaded": 0, "fallback": 0, "skipped": 0, "failed": 0}
    for cycle_dt in cycles:
        source_mode = "auto"
        remaining_hours = forecast_hours

        if args.probe_f001 and args.fallback_previous_cycle and args.start_fhour in forecast_hours:
            first_hour = args.start_fhour
            print(f"[PROBE] {cycle_dt:%Y%m%d%H} 先检查 f{first_hour:03d}，判断该起报点是否已发布")
            result = sync_target(args, cycle_dt, first_hour, source_mode="auto")
            update_counts(result, counts)

            if result in {"downloaded_preferred", "skipped_preferred", "printed"}:
                source_mode = "preferred"
                print(f"[PROBE] {cycle_dt:%Y%m%d%H} f{first_hour:03d} 可用，后续时效直接下载该起报点")
            else:
                source_mode = "fallback"
                print(f"[PROBE] {cycle_dt:%Y%m%d%H} f{first_hour:03d} 不可用，后续时效直接使用前一轮 fallback")

            remaining_hours = [h for h in forecast_hours if h != first_hour]

        for forecast_hour in forecast_hours:
            if forecast_hour not in remaining_hours:
                continue
            result = sync_target(args, cycle_dt, forecast_hour, source_mode=source_mode)
            update_counts(result, counts)

    print_summary(counts, len(cycles) * len(forecast_hours))


def download_range(args):
    forecast_hours = list(range(args.start_fhour, args.end_fhour + 1))
    if args.start_cycle:
        start_dt = parse_utc_hour(args.start_cycle)
    else:
        start_dt = parse_month_start(args.start_month)
    if args.end_cycle:
        end_dt = parse_utc_hour(args.end_cycle)
    else:
        end_dt = month_end(args.end_month)

    args.global_region = False
    half_box = args.box_degrees / 2
    args.toplat = args.lat + half_box
    args.bottomlat = args.lat - half_box
    args.leftlon = args.lon - half_box
    args.rightlon = args.lon + half_box

    cycles = list(build_cycles_between(start_dt, end_dt))
    print("=" * 100)
    print(f"Cycles UTC: {cycles[0]:%Y%m%d%H} -> {cycles[-1]:%Y%m%d%H} ({len(cycles)} cycles)")
    print(f"Forecast hours: {forecast_hours}")
    print(f"Point/subregion: lat={args.lat}, lon={args.lon}, box={args.box_degrees} deg")
    print(f"Output dir: {args.output_dir}")

    counts = {"downloaded": 0, "fallback": 0, "skipped": 0, "failed": 0}
    for cycle_dt in cycles:
        for forecast_hour in forecast_hours:
            target = output_path(args.output_dir, cycle_dt, forecast_hour, args.global_region)
            if is_valid_file(target, args.min_bytes):
                print(f"[SKIP] {target.name} already exists")
                update_counts("skipped_preferred", counts)
                continue

            item = {
                "kind": "preferred",
                "source_cycle_dt": cycle_dt,
                "source_forecast_hour": forecast_hour,
                "target_cycle_dt": cycle_dt,
                "target_forecast_hour": forecast_hour,
            }
            variables, levels = requested_variables_and_levels(args)
            url = build_nomads_url(
                cycle_dt=cycle_dt,
                forecast_hour=forecast_hour,
                global_region=args.global_region,
                top_lat=args.toplat,
                bottom_lat=args.bottomlat,
                left_lon=args.leftlon,
                right_lon=args.rightlon,
                variables=variables,
                levels=levels,
            )
            print(f"[PLAN] {format_plan_item(item)}")
            if args.print_url_only:
                print(f"[TARGET] {target}")
                print(url)
                update_counts("printed", counts)
                continue

            ok = download(url, target, retries=args.retries, timeout=args.timeout, min_bytes=args.min_bytes)
            if ok:
                write_grib_meta(target, item, args)
                update_counts("downloaded_preferred", counts)
            else:
                update_counts("failed", counts)

    print_summary(counts, len(cycles) * len(forecast_hours))


def parse_forecast_hours(value):
    hours = []
    for item in value.split(","):
        item = item.strip().lower().removeprefix("f")
        if not item:
            continue
        hours.append(int(item))
    return sorted(set(hours))


def build_gdex_url(cycle_dt, forecast_hour, base_url=GDEX_BASE_URL):
    year = cycle_dt.strftime("%Y")
    ymd = cycle_dt.strftime("%Y%m%d")
    ymdh = cycle_dt.strftime("%Y%m%d%H")
    return f"{base_url.rstrip('/')}/{year}/{ymd}/gfs.0p25.{ymdh}.f{forecast_hour:03d}.grib2"


def gdex_output_path(output_dir, cycle_dt, forecast_hour):
    ymd = cycle_dt.strftime("%Y%m%d")
    ymdh = cycle_dt.strftime("%Y%m%d%H")
    return Path(output_dir) / ymd / f"gfs.0p25.{ymdh}.f{forecast_hour:03d}.grib2"


def write_gdex_meta(path, cycle_dt, forecast_hour, args):
    write_meta(
        path,
        {
            "source": "UCAR GDEX d084001 NCEP GFS 0.25 Degree Global Forecast Grids",
            "url": build_gdex_url(cycle_dt, forecast_hour, args.gdex_base_url),
            "cycle_utc": cycle_dt.strftime("%Y%m%d%H"),
            "forecast_hour": forecast_hour,
            "valid_time_utc": (cycle_dt + timedelta(hours=forecast_hour)).strftime("%Y%m%d%H"),
            "grid": "0.25 degree global 1440x721",
            "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )


def download_gdex(args):
    start_dt = parse_utc_hour(args.start_cycle) if args.start_cycle else parse_utc_hour(f"{args.start_date}00")
    end_dt = parse_utc_hour(args.end_cycle) if args.end_cycle else parse_utc_hour(f"{args.end_date}18")
    forecast_hours = parse_forecast_hours(args.forecast_hours)
    cycles = list(build_cycles_between(start_dt, end_dt))

    print("=" * 100)
    print(f"GDEX URL base: {args.gdex_base_url}")
    print(f"Cycles UTC: {cycles[0]:%Y%m%d%H} -> {cycles[-1]:%Y%m%d%H} ({len(cycles)} cycles)")
    print(f"Forecast hours: {forecast_hours}")
    print(f"Output dir: {args.output_dir}")

    counts = {"downloaded": 0, "fallback": 0, "skipped": 0, "failed": 0}
    for cycle_dt in cycles:
        for forecast_hour in forecast_hours:
            if (
                args.start_forecast_hour is not None
                and cycle_dt == start_dt
                and forecast_hour < args.start_forecast_hour
            ):
                continue
            target = gdex_output_path(args.output_dir, cycle_dt, forecast_hour)
            if is_valid_file(target, args.min_bytes):
                print(f"[SKIP] {target.name} already exists")
                update_counts("skipped_preferred", counts)
                continue

            url = build_gdex_url(cycle_dt, forecast_hour, args.gdex_base_url)
            print(f"[PLAN] source={cycle_dt:%Y%m%d%H} f{forecast_hour:03d}")
            if args.print_url_only:
                print(f"[TARGET] {target}")
                print(url)
                update_counts("printed", counts)
                continue

            ok = download(url, target, retries=args.retries, timeout=args.timeout, min_bytes=args.min_bytes)
            if ok:
                write_gdex_meta(target, cycle_dt, forecast_hour, args)
                update_counts("downloaded_preferred", counts)
            else:
                update_counts("failed", counts)

    print_summary(counts, len(cycles) * len(forecast_hours))


def wgrib2_regex_for_windcheck():
    levels = [
        "surface",
        *ABOVE_GROUND_WGRIB2_LEVELS,
        *[f"{level} mb" for level in WIND_CHECK_PRESSURE_LEVELS_HPA],
    ]
    var_part = "|".join(WIND_CHECK_VARS)
    level_part = "|".join(levels)
    return rf":({var_part}):({level_part}):"


def subset_one_grib_with_wgrib2(source, target, args):
    wgrib2 = args.wgrib2 or shutil.which("wgrib2")
    if not wgrib2:
        raise SystemExit(
            "裁剪 GRIB2 需要 wgrib2。请先安装 wgrib2，或用 --wgrib2 指定 wgrib2.exe 路径。"
        )

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    regex = args.match_regex or wgrib2_regex_for_windcheck()
    small_grib = target.with_suffix(target.suffix + ".vars.tmp")
    cmd_vars = [wgrib2, str(source), "-match", regex, "-grib", str(small_grib)]
    print(f"[SUBSET] vars/levels {Path(source).name}")
    subprocess.run(cmd_vars, check=True)

    lon1 = args.leftlon % 360
    lon2 = args.rightlon % 360
    cmd_box = [
        wgrib2,
        str(small_grib),
        "-small_grib",
        f"{lon1}:{lon2}",
        f"{args.bottomlat}:{args.toplat}",
        str(target),
    ]
    print(f"[SUBSET] region lon={lon1}:{lon2}, lat={args.bottomlat}:{args.toplat}")
    try:
        subprocess.run(cmd_box, check=True)
    finally:
        if small_grib.exists():
            small_grib.unlink()


def subset_gdex(args):
    source_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    files = sorted(source_dir.rglob("gfs.0p25.*.f*.grib2"))
    if not files:
        raise SystemExit(f"未在 {source_dir} 找到 GDEX GRIB2 文件。")

    print("=" * 100)
    print(f"Input dir: {source_dir}")
    print(f"Output dir: {output_dir}")
    print(f"Files: {len(files)}")
    print(f"Variables/levels regex: {args.match_regex or wgrib2_regex_for_windcheck()}")
    print(f"Region: top={args.toplat}, bottom={args.bottomlat}, left={args.leftlon}, right={args.rightlon}")

    done = 0
    skipped = 0
    failed = 0
    for source in files:
        rel = source.relative_to(source_dir)
        target = output_dir / rel
        if is_valid_file(target, args.min_bytes):
            print(f"[SKIP] {target}")
            skipped += 1
            continue
        try:
            subset_one_grib_with_wgrib2(source, target, args)
            meta = read_meta(source)
            meta.update(
                {
                    "subset_source": str(source),
                    "subset_regex": args.match_regex or wgrib2_regex_for_windcheck(),
                    "subset_region": {
                        "toplat": args.toplat,
                        "bottomlat": args.bottomlat,
                        "leftlon": args.leftlon,
                        "rightlon": args.rightlon,
                    },
                    "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
            write_meta(target, meta)
            done += 1
        except subprocess.CalledProcessError as exc:
            print(f"[ERROR] {source}: {exc}", file=sys.stderr)
            failed += 1

    print(f"[SUMMARY] total={len(files)}, subset={done}, skipped={skipped}, failed={failed}")


def print_summary(counts, total):
    print(
        f"[SUMMARY] total={total}, downloaded={counts['downloaded']}, "
        f"fallback={counts['fallback']}, skipped={counts['skipped']}, failed={counts['failed']}"
    )


def load_observations(obs_dir):
    rows = []
    for path in sorted(Path(obs_dir).glob("WindData_product_*_10min.xlsx")):
        print(f"[OBS] {path.name}")
        raw = pd.read_excel(path, header=None, engine="openpyxl")
        heights = raw.iloc[0, 1:].to_numpy()
        data = raw.iloc[1:].copy()
        times_bj = pd.to_datetime(data.iloc[:, 0], errors="coerce")

        for col in range(1, raw.shape[1], 3):
            if col >= raw.shape[1]:
                break
            height = pd.to_numeric(pd.Series([heights[col - 1]]), errors="coerce").iloc[0]
            if not np.isfinite(height):
                continue

            speed = pd.to_numeric(data.iloc[:, col], errors="coerce").replace(-999, np.nan)
            direction = pd.Series(np.nan, index=data.index, dtype=float)
            if col + 1 < raw.shape[1]:
                direction = pd.to_numeric(data.iloc[:, col + 1], errors="coerce").replace(-999, np.nan)
            part = pd.DataFrame(
                {
                    "time_bj": times_bj,
                    "time_utc": times_bj.dt.tz_localize(BEIJING_TZ).dt.tz_convert(timezone.utc),
                    "height_m": float(height),
                    "obs_speed": speed.astype(float),
                    "obs_dir": direction.astype(float),
                }
            )
            rows.append(part.dropna(subset=["time_utc", "obs_speed"]))

    if not rows:
        return pd.DataFrame(columns=["time_bj", "time_utc", "height_m", "obs_speed", "obs_dir"])

    obs = pd.concat(rows, ignore_index=True)
    obs["valid_time_utc"] = obs["time_utc"].dt.floor("h")
    return obs


def open_grib_dataset(path, filter_by_keys=None):
    try:
        import xarray as xr
        import cfgrib  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "读取 GRIB2 需要安装 cfgrib/eccodes。建议在当前环境执行："
            "conda install -c conda-forge cfgrib eccodes"
        ) from exc

    return xr.open_dataset(path, engine="cfgrib", backend_kwargs={"filter_by_keys": filter_by_keys or {}})


def pick_var(ds, candidates):
    for name in candidates:
        if name in ds:
            return ds[name]
    raise KeyError(f"GRIB 中缺少变量，候选名: {candidates}; 实际变量: {list(ds.data_vars)}")


def scalar_at_point(da, lat, lon):
    lon_value = lon % 360
    selector = {}
    if "latitude" in da.coords:
        selector["latitude"] = lat
    if "longitude" in da.coords:
        selector["longitude"] = lon_value
    if selector:
        da = da.sel(selector, method="nearest")
    arr = np.asarray(da.squeeze())
    return float(arr.reshape(-1)[0])


def read_gfs_profile(path, lat, lon):
    ds_iso = open_grib_dataset(path, {"typeOfLevel": "isobaricInhPa"})
    ds_surface = open_grib_dataset(path, {"typeOfLevel": "surface"})

    gh = pick_var(ds_iso, ["gh", "hgt"])
    u = pick_var(ds_iso, ["u", "u10", "ugrd"])
    v = pick_var(ds_iso, ["v", "v10", "vgrd"])
    terrain = pick_var(ds_surface, ["orog", "gh", "hgt"])

    pressure_coord = "isobaricInhPa" if "isobaricInhPa" in gh.coords else "level"
    terrain_m = scalar_at_point(terrain, lat, lon)
    records = []
    for pressure in np.asarray(gh[pressure_coord].values, dtype=float):
        one = {pressure_coord: pressure}
        h_msl = scalar_at_point(gh.sel(one), lat, lon)
        uu = scalar_at_point(u.sel(one), lat, lon)
        vv = scalar_at_point(v.sel(one), lat, lon)
        records.append(
            {
                "pressure_hpa": pressure,
                "height_agl_m": h_msl - terrain_m,
                "u": uu,
                "v": vv,
                "gfs_speed": math.hypot(uu, vv),
                "gfs_dir": wind_direction_from_uv(uu, vv),
            }
        )

    profile = pd.DataFrame(records).sort_values("height_agl_m")
    valid_time = pd.Timestamp(ds_iso.valid_time.values).tz_localize(timezone.utc)
    return valid_time, profile


def wind_direction_from_uv(u, v):
    return (270.0 - math.degrees(math.atan2(v, u))) % 360.0


def circular_direction_diff(pred_dir, obs_dir):
    return ((pred_dir - obs_dir + 180.0) % 360.0) - 180.0


def circular_mean_deg(values):
    arr = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if len(arr) == 0:
        return np.nan
    radians = np.deg2rad(arr)
    sin_mean = np.mean(np.sin(radians))
    cos_mean = np.mean(np.cos(radians))
    if np.isclose(sin_mean, 0.0) and np.isclose(cos_mean, 0.0):
        return np.nan
    return float(np.rad2deg(np.arctan2(sin_mean, cos_mean)) % 360.0)


def interpolate_profile(profile, obs_heights):
    x = profile["height_agl_m"].to_numpy(dtype=float)
    speed = profile["gfs_speed"].to_numpy(dtype=float)
    u = profile["u"].to_numpy(dtype=float)
    v = profile["v"].to_numpy(dtype=float)
    pressure = profile["pressure_hpa"].to_numpy(dtype=float)
    order = np.argsort(x)
    x = x[order]
    speed = speed[order]
    u = u[order]
    v = v[order]
    pressure = pressure[order]

    good = np.isfinite(x) & np.isfinite(speed) & np.isfinite(u) & np.isfinite(v) & np.isfinite(pressure)
    x = x[good]
    speed = speed[good]
    u = u[good]
    v = v[good]
    pressure = pressure[good]
    heights = np.asarray(obs_heights, dtype=float)
    if len(x) < 2:
        return pd.DataFrame(
            {
                "height_m": heights,
                "gfs_speed": np.nan,
                "gfs_dir": np.nan,
                "gfs_u": np.nan,
                "gfs_v": np.nan,
                "pressure_hpa": np.nan,
            }
        )

    interp_u = np.interp(heights, x, u, left=np.nan, right=np.nan)
    interp_v = np.interp(heights, x, v, left=np.nan, right=np.nan)
    interp_speed = np.hypot(interp_u, interp_v)
    interp_dir = [
        wind_direction_from_uv(uu, vv) if np.isfinite(uu) and np.isfinite(vv) else np.nan
        for uu, vv in zip(interp_u, interp_v)
    ]

    return pd.DataFrame(
        {
            "height_m": heights,
            "gfs_speed": interp_speed,
            "gfs_dir": interp_dir,
            "gfs_u": interp_u,
            "gfs_v": interp_v,
            "pressure_hpa": np.interp(heights, x, pressure, left=np.nan, right=np.nan),
        }
    )


def infer_valid_time_from_path(path):
    match = re.search(r"gfs\.0p25\.(\d{10})\.f(\d{3})\.grib2$", Path(path).name)
    if not match:
        return None
    cycle_dt = parse_utc_hour(match.group(1))
    return cycle_dt + timedelta(hours=int(match.group(2)))


def read_all_gfs_predictions(gfs_dir, heights, lat, lon, valid_start_utc=None, valid_end_utc=None):
    rows = []
    paths = sorted(Path(gfs_dir).rglob("*.grib2"))
    for path in paths:
        inferred_valid_time = infer_valid_time_from_path(path)
        if inferred_valid_time is not None:
            if valid_start_utc is not None and inferred_valid_time < valid_start_utc:
                continue
            if valid_end_utc is not None and inferred_valid_time >= valid_end_utc:
                continue

        print(f"[GFS] {path.name}")
        valid_time, profile = read_gfs_profile(path, lat, lon)
        if valid_start_utc is not None and valid_time < pd.Timestamp(valid_start_utc):
            continue
        if valid_end_utc is not None and valid_time >= pd.Timestamp(valid_end_utc):
            continue

        pred = interpolate_profile(profile, heights)
        pred["valid_time_utc"] = valid_time
        meta = read_meta(path)
        pred["cycle_utc"] = meta.get("source_cycle_utc") or meta.get("cycle_utc")
        pred["forecast_hour"] = meta.get("source_forecast_hour") or meta.get("forecast_hour")
        rows.append(pred)

    if not rows:
        return pd.DataFrame(columns=["valid_time_utc", "height_m", "gfs_speed", "pressure_hpa"])
    return pd.concat(rows, ignore_index=True)


def compute_metrics(pairs):
    err = pairs["gfs_speed"] - pairs["obs_speed"]
    mse = float(np.mean(np.square(err)))
    rmse = float(np.sqrt(mse))
    mae = float(np.mean(np.abs(err)))
    bias = float(np.mean(err))
    corr = float(pairs["gfs_speed"].corr(pairs["obs_speed"])) if len(pairs) > 1 else np.nan
    result = {
        "count": int(len(pairs)),
        "mse": mse,
        "rmse": rmse,
        "mae": mae,
        "bias": bias,
        "corr": corr,
    }
    if "dir_diff" in pairs:
        dir_abs = pairs["dir_diff"].abs().dropna()
        result["dir_count"] = int(len(dir_abs))
        result["dir_mae_deg"] = float(dir_abs.mean()) if len(dir_abs) else np.nan
        result["dir_bias_deg"] = float(pairs["dir_diff"].dropna().mean()) if len(dir_abs) else np.nan
    return pd.Series(result)


def beijing_day_window(date_text):
    start_bj = datetime.strptime(date_text, "%Y%m%d").replace(tzinfo=BEIJING_TZ)
    end_bj = start_bj + timedelta(days=1)
    return start_bj, end_bj, start_bj.astimezone(timezone.utc), end_bj.astimezone(timezone.utc)


def compare(args):
    obs = load_observations(args.obs_dir)
    if obs.empty:
        raise SystemExit("未读取到有效实测风速。")

    valid_start_utc = None
    valid_end_utc = None
    if not args.all_dates and args.compare_date:
        start_bj, end_bj, valid_start_utc, valid_end_utc = beijing_day_window(args.compare_date)
        obs = obs[obs["time_bj"].dt.strftime("%Y%m%d") == args.compare_date].copy()
        print(
            f"[COMPARE] Beijing date {args.compare_date}: "
            f"{start_bj:%Y-%m-%d %H:%M %Z} -> {end_bj:%Y-%m-%d %H:%M %Z}, "
            f"UTC window {valid_start_utc:%Y-%m-%d %H:%M} -> {valid_end_utc:%Y-%m-%d %H:%M}"
        )
        if obs.empty:
            raise SystemExit(f"实测数据中没有北京时间 {args.compare_date} 的有效记录。")

    if args.obs_aggregation == "hourly_mean":
        grouped = obs.groupby(["valid_time_utc", "height_m"], as_index=False)
        obs_match = grouped.agg(obs_speed=("obs_speed", "mean"), time_bj=("time_bj", "max"))
        obs_dir = (
            obs.groupby(["valid_time_utc", "height_m"])["obs_dir"]
            .apply(circular_mean_deg)
            .reset_index(name="obs_dir")
        )
        obs_match = obs_match.merge(obs_dir, on=["valid_time_utc", "height_m"], how="left")
    else:
        wanted_minutes = {0}
        obs_match = obs[obs["time_bj"].dt.minute.isin(wanted_minutes)].copy()

    heights = sorted(obs_match["height_m"].dropna().unique())
    pred = read_all_gfs_predictions(
        args.gfs_dir,
        heights,
        args.lat,
        args.lon,
        valid_start_utc=valid_start_utc,
        valid_end_utc=valid_end_utc,
    )
    if pred.empty:
        raise SystemExit("未读取到 GFS windcheck GRIB2 文件。")

    pairs = pred.merge(obs_match, on=["valid_time_utc", "height_m"], how="inner")
    pairs = pairs.dropna(subset=["gfs_speed", "obs_speed"])
    if pairs.empty:
        raise SystemExit("GFS 与实测数据没有匹配上的时间和高度。请检查 UTC/北京时间、下载时段和文件目录。")

    pairs["speed_diff"] = pairs["gfs_speed"] - pairs["obs_speed"]
    pairs["dir_diff"] = circular_direction_diff(pairs["gfs_dir"], pairs["obs_dir"])
    pairs["abs_dir_diff"] = pairs["dir_diff"].abs()

    summary = compute_metrics(pairs).to_frame().T
    by_height = pairs.groupby("height_m").apply(compute_metrics, include_groups=False).reset_index()
    by_fhour = pairs.groupby("forecast_hour").apply(compute_metrics, include_groups=False).reset_index()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs.to_csv(out_dir / "gfs_obs_pairs.csv", index=False, encoding="utf-8-sig")
    pairs.to_csv(out_dir / "gfs_obs_speed_direction_pairs.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out_dir / "gfs_obs_metrics_summary.csv", index=False, encoding="utf-8-sig")
    by_height.to_csv(out_dir / "gfs_obs_metrics_by_height.csv", index=False, encoding="utf-8-sig")
    by_fhour.to_csv(out_dir / "gfs_obs_metrics_by_forecast_hour.csv", index=False, encoding="utf-8-sig")

    print("[METRICS] summary")
    print(summary.to_string(index=False))
    show_cols = [
        "valid_time_utc",
        "time_bj",
        "height_m",
        "forecast_hour",
        "obs_speed",
        "gfs_speed",
        "speed_diff",
        "obs_dir",
        "gfs_dir",
        "dir_diff",
        "abs_dir_diff",
        "pressure_hpa",
    ]
    print("[SAMPLE] speed/direction pairs")
    print(pairs[show_cols].sort_values(["valid_time_utc", "height_m"]).head(args.sample_rows).to_string(index=False))
    print(f"[OUTPUT] {out_dir}")


def add_common_download_args(parser):
    parser.add_argument("--output-dir", default="./data/gfs_hourly_windcheck")
    parser.add_argument("--start-fhour", type=int, default=1)
    parser.add_argument("--end-fhour", type=int, default=6, help="每个起报点下载 f001 到 f006")
    parser.add_argument("--lat", type=float, default=31.0)
    parser.add_argument("--lon", type=float, default=118.5)
    parser.add_argument("--box-degrees", type=float, default=0.25, help="围绕目标点下载的小区域边长，单位度")
    parser.add_argument("--global-region", action="store_true", default=False, help="下载全球范围")
    parser.add_argument(
        "--wind-map-only",
        action="store_true",
        help="仅下载地图展示所需的 10/20/30/40/50/80/100m AGL UGRD/VGRD，适合全球范围",
    )
    parser.add_argument("--subregion", action="store_true", help="使用子区域参数")
    parser.add_argument("--toplat", type=float, default=31.125)
    parser.add_argument("--bottomlat", type=float, default=30.875)
    parser.add_argument("--leftlon", type=float, default=118.375)
    parser.add_argument("--rightlon", type=float, default=118.625)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--min-bytes", type=int, default=1024)
    parser.add_argument("--print-url-only", action="store_true")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "同步 GFS 0.25 Degree Hourly 风场/HGT 数据，并与测风激光雷达实测风速对比。"
            "HGT 等压面用于建立压强-高度对应关系。"
        )
    )
    subparsers = parser.add_subparsers(dest="command")

    realtime = subparsers.add_parser("realtime", help="原实时同步模式")
    add_common_download_args(realtime)
    realtime.set_defaults(func=run_realtime)
    realtime.add_argument("--cycle-count", type=int, default=6, help="回溯起报点个数")
    realtime.add_argument("--fallback-previous-cycle", action="store_true", default=True)
    realtime.add_argument("--no-fallback-previous-cycle", dest="fallback_previous_cycle", action="store_false")
    realtime.add_argument("--probe-f001", action="store_true", default=True)
    realtime.add_argument("--no-probe-f001", dest="probe_f001", action="store_false")
    realtime.add_argument("--delay-hours", type=int, default=0)
    realtime.add_argument("--now", default=None, help="调试用，指定当前 UTC 小时 YYYYMMDDHH")
    realtime.add_argument("--watch", action="store_true", help="持续监测并自动同步")
    realtime.add_argument("--interval-minutes", type=int, default=20, help="watch 模式检查间隔")

    range_parser = subparsers.add_parser("download-range", help="下载固定历史时段 f001-f006")
    add_common_download_args(range_parser)
    range_parser.set_defaults(func=download_range)
    range_parser.add_argument("--start-month", default="202511")
    range_parser.add_argument("--end-month", default="202602")
    range_parser.add_argument("--start-cycle", default=None, help="UTC 起报点 YYYYMMDDHH，优先于 start-month")
    range_parser.add_argument("--end-cycle", default=None, help="UTC 起报点 YYYYMMDDHH，优先于 end-month")
    range_parser.add_argument("--fallback-previous-cycle", action="store_true", default=False)

    gdex_parser = subparsers.add_parser("download-gdex", help="下载 UCAR/GDEX GFS 0.25 历史全球 GRIB2")
    gdex_parser.set_defaults(func=download_gdex)
    gdex_parser.add_argument("--gdex-base-url", default=GDEX_BASE_URL)
    gdex_parser.add_argument("--output-dir", default="./data/gdex_gfs_0p25_global")
    gdex_parser.add_argument("--start-date", default="20251101", help="UTC 日期 YYYYMMDD")
    gdex_parser.add_argument("--end-date", default="20260228", help="UTC 日期 YYYYMMDD")
    gdex_parser.add_argument("--start-cycle", default=None, help="UTC 起报点 YYYYMMDDHH，优先于 start-date")
    gdex_parser.add_argument("--end-cycle", default=None, help="UTC 起报点 YYYYMMDDHH，优先于 end-date")
    gdex_parser.add_argument("--forecast-hours", default="3,6", help="逗号分隔，例如 3,6 或 f003,f006")
    gdex_parser.add_argument("--start-forecast-hour", type=int, default=None,
                             help="仅对 start-cycle 生效；从该预报时效开始，例如 6 表示从 f006 续下")
    gdex_parser.add_argument("--retries", type=int, default=3)
    gdex_parser.add_argument("--timeout", type=int, default=600)
    gdex_parser.add_argument("--min-bytes", type=int, default=10 * 1024 * 1024)
    gdex_parser.add_argument("--print-url-only", action="store_true")

    subset_parser = subparsers.add_parser("subset-gdex", help="用 wgrib2 从 GDEX 全球 GRIB2 裁剪变量/层次/区域")
    subset_parser.set_defaults(func=subset_gdex)
    subset_parser.add_argument("--input-dir", default="./data/gdex_gfs_0p25_global")
    subset_parser.add_argument("--output-dir", default="./data/gdex_gfs_0p25_windcheck")
    subset_parser.add_argument("--toplat", type=float, default=31.125)
    subset_parser.add_argument("--bottomlat", type=float, default=30.875)
    subset_parser.add_argument("--leftlon", type=float, default=118.375)
    subset_parser.add_argument("--rightlon", type=float, default=118.625)
    subset_parser.add_argument("--match-regex", default=None, help="自定义 wgrib2 -match 正则；默认抽取 HGT/PRES/UGRD/VGRD 及所需层次")
    subset_parser.add_argument("--wgrib2", default=None, help="wgrib2 或 wgrib2.exe 路径")
    subset_parser.add_argument("--min-bytes", type=int, default=1024)

    compare_parser = subparsers.add_parser("compare", help="读取 GFS GRIB2 与雷达 Excel 并输出误差指标")
    compare_parser.set_defaults(func=compare)
    compare_parser.add_argument("--gfs-dir", default="./data/gdex_gfs_0p25_windcheck")
    compare_parser.add_argument("--obs-dir", default="./202511~202602")
    compare_parser.add_argument("--output-dir", default="./data/gfs_wind_validation")
    compare_parser.add_argument("--lat", type=float, default=31.0)
    compare_parser.add_argument("--lon", type=float, default=118.5)
    compare_parser.add_argument("--obs-aggregation", choices=["nearest_hour", "hourly_mean"], default="nearest_hour")
    compare_parser.add_argument("--compare-date", default="20251101", help="先验证的北京时间日期 YYYYMMDD，默认 20251101")
    compare_parser.add_argument("--all-dates", action="store_true", help="不按单日过滤，验证所有可匹配数据")
    compare_parser.add_argument("--sample-rows", type=int, default=30, help="终端打印的逐时逐高度样例行数")

    return parser


def run_realtime(args):
    if args.subregion:
        args.global_region = False

    if not args.global_region:
        half_box = args.box_degrees / 2
        args.toplat = args.lat + half_box
        args.bottomlat = args.lat - half_box
        args.leftlon = args.lon - half_box
        args.rightlon = args.lon + half_box

    while True:
        sync_once(args)
        if not args.watch:
            break
        sleep_seconds = args.interval_minutes * 60
        print(f"[WATCH] sleep {args.interval_minutes} minutes...")
        time.sleep(sleep_seconds)


def main():
    parser = build_parser()
    argv = sys.argv[1:]
    commands = {"realtime", "download-range", "download-gdex", "subset-gdex", "compare"}
    if not argv or argv[0] not in commands:
        # Backward compatible default: original script ran realtime once.
        argv = ["realtime"] + argv
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
