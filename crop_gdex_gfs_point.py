import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


PRESSURE_LEVELS_HPA = [
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
DEFAULT_VARS = ["HGT", "PRES", "UGRD", "VGRD"]
ABOVE_GROUND_WIND_LEVELS = [
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


def parse_utc_hour(value):
    return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def build_cycles(start_dt, end_dt):
    cur = start_dt
    while cur <= end_dt:
        yield cur
        cur += timedelta(hours=6)


def parse_forecast_hours(value):
    hours = []
    for item in value.split(","):
        item = item.strip().lower().removeprefix("f")
        if item:
            hours.append(int(item))
    return sorted(set(hours))


def expected_items(start_dt, end_dt, forecast_hours):
    items = []
    for cycle_dt in build_cycles(start_dt, end_dt):
        for forecast_hour in forecast_hours:
            items.append((cycle_dt, forecast_hour))
    return items


def grib_path(root, cycle_dt, forecast_hour):
    ymd = cycle_dt.strftime("%Y%m%d")
    ymdh = cycle_dt.strftime("%Y%m%d%H")
    return Path(root) / ymd / f"gfs.0p25.{ymdh}.f{forecast_hour:03d}.grib2"


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


def build_match_regex():
    levels = [
        "surface",
        *ABOVE_GROUND_WIND_LEVELS,
        *[f"{level} mb" for level in PRESSURE_LEVELS_HPA],
    ]
    return rf":({'|'.join(DEFAULT_VARS)}):({'|'.join(levels)}):"


def find_wgrib2(path):
    if path:
        return path
    found = shutil.which("wgrib2")
    if not found:
        raise SystemExit("未找到 wgrib2。请安装 wgrib2，或使用 --wgrib2 指定 wgrib2.exe 路径。")
    return found


def build_subprocess_env(args):
    env = os.environ.copy()
    lib_dirs = []
    if args.library_dir:
        lib_dirs.append(args.library_dir)
    conda_prefix = env.get("CONDA_PREFIX")
    if conda_prefix:
        lib_dirs.append(str(Path(conda_prefix) / "lib"))
    if args.wgrib2:
        wgrib2_path = Path(args.wgrib2).resolve()
        lib_dirs.append(str(wgrib2_path.parent.parent / "lib"))

    existing = env.get("LD_LIBRARY_PATH")
    merged = []
    for item in lib_dirs:
        if item and item not in merged:
            merged.append(item)
    if existing:
        merged.append(existing)
    if merged:
        env["LD_LIBRARY_PATH"] = ":".join(merged)
    return env


def list_netcdf_libraries(args):
    candidates = []
    env = os.environ.copy()
    if args.library_dir:
        candidates.append(Path(args.library_dir))
    if env.get("CONDA_PREFIX"):
        candidates.append(Path(env["CONDA_PREFIX"]) / "lib")
    if args.wgrib2:
        candidates.append(Path(args.wgrib2).resolve().parent.parent / "lib")

    seen = set()
    found = []
    for directory in candidates:
        if directory in seen or not directory.exists():
            continue
        seen.add(directory)
        found.extend(str(path) for path in sorted(directory.glob("libnetcdf.so*")))
    return found


def run_doctor(args):
    wgrib2 = find_wgrib2(args.wgrib2)
    env = build_subprocess_env(args)
    print("=" * 100)
    print(f"wgrib2: {wgrib2}")
    print(f"CONDA_PREFIX: {os.environ.get('CONDA_PREFIX', '')}")
    print(f"LD_LIBRARY_PATH used: {env.get('LD_LIBRARY_PATH', '')}")
    print("候选 libnetcdf:")
    libraries = list_netcdf_libraries(args)
    if libraries:
        for item in libraries:
            print(f"  {item}")
    else:
        print("  未找到 libnetcdf.so*")

    print("\n[wgrib2 -version]")
    result = subprocess.run([wgrib2, "-version"], capture_output=True, text=True, env=env)
    print((result.stdout or "").strip())
    print((result.stderr or "").strip(), file=sys.stderr)
    print(f"returncode={result.returncode}")

    ldd = shutil.which("ldd")
    if ldd:
        print("\n[ldd wgrib2]")
        ldd_result = subprocess.run([ldd, wgrib2], capture_output=True, text=True, env=env)
        print((ldd_result.stdout or "").strip())
        print((ldd_result.stderr or "").strip(), file=sys.stderr)
        print(f"returncode={ldd_result.returncode}")


def check_wgrib2(args):
    wgrib2 = find_wgrib2(args.wgrib2)
    try:
        result = subprocess.run(
            [wgrib2, "-version"],
            check=False,
            capture_output=True,
            text=True,
            env=build_subprocess_env(args),
        )
    except OSError as exc:
        raise SystemExit(f"wgrib2 无法启动: {wgrib2}\n{exc}") from exc

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    detail = stderr or stdout
    version_ok = bool(stdout) and "error while loading shared libraries" not in detail
    if result.returncode != 0 and not version_ok:
        libraries = list_netcdf_libraries(args)
        lib_text = "\n".join(f"  {item}" for item in libraries) if libraries else "  未在候选 lib 目录找到 libnetcdf.so*"
        raise SystemExit(
            "wgrib2 预检失败，裁剪已停止。\n"
            f"wgrib2: {wgrib2}\n"
            f"LD_LIBRARY_PATH: {build_subprocess_env(args).get('LD_LIBRARY_PATH', '')}\n"
            f"候选 libnetcdf:\n{lib_text}\n"
            f"错误信息: {detail}\n\n"
            "如果仍报 libnetcdf.so.13，说明当前 wgrib2 需要的 netcdf SONAME 与环境里的 libnetcdf 不匹配。"
            "建议重新安装同一 channel 的匹配包：\n"
            "  conda install -c conda-forge --force-reinstall wgrib2 libnetcdf\n"
            "或改用一个动态库完整的 wgrib2，并通过 --wgrib2 指定路径。"
        )

    print(f"[CHECK] wgrib2 ok: {wgrib2} ({stdout or 'version checked'})")
    return wgrib2


def crop_one(source, target, args):
    wgrib2 = args.wgrib2_checked
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_vars = target.with_suffix(target.suffix + ".vars.tmp")
    tmp_out = target.with_suffix(target.suffix + ".tmp")
    regex = args.match_regex or build_match_regex()
    lon1 = args.leftlon % 360
    lon2 = args.rightlon % 360

    if tmp_vars.exists():
        tmp_vars.unlink()
    if tmp_out.exists():
        tmp_out.unlink()

    env = build_subprocess_env(args)
    subprocess.run([wgrib2, str(source), "-match", regex, "-grib", str(tmp_vars)], check=True, env=env)
    subprocess.run(
        [
            wgrib2,
            str(tmp_vars),
            "-small_grib",
            f"{lon1}:{lon2}",
            f"{args.bottomlat}:{args.toplat}",
            str(tmp_out),
        ],
        check=True,
        env=env,
    )
    tmp_out.replace(target)
    if tmp_vars.exists():
        tmp_vars.unlink()

    meta = read_meta(source)
    meta.update(
        {
            "crop_source": str(source),
            "crop_region": {
                "bottomlat": args.bottomlat,
                "toplat": args.toplat,
                "leftlon": args.leftlon,
                "rightlon": args.rightlon,
            },
            "crop_match_regex": regex,
            "cropped_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    write_meta(target, meta)


def safe_delete(path):
    path = Path(path)
    if path.exists():
        path.unlink()
    mp = meta_path(path)
    if mp.exists():
        mp.unlink()


def process_once(items, args):
    cropped = 0
    skipped = 0
    waiting = []
    failed = 0

    for index, (cycle_dt, forecast_hour) in enumerate(items):
        source = grib_path(args.input_dir, cycle_dt, forecast_hour)
        target = grib_path(args.output_dir, cycle_dt, forecast_hour)
        next_source = None
        if index + 1 < len(items):
            next_cycle, next_fhour = items[index + 1]
            next_source = grib_path(args.input_dir, next_cycle, next_fhour)

        if target.exists() and target.stat().st_size >= args.min_bytes:
            if args.delete_original and source.exists():
                safe_delete(source)
            skipped += 1
            continue

        if not source.exists():
            waiting.append(f"missing current {source}")
            continue

        if source.with_suffix(source.suffix + ".tmp").exists():
            waiting.append(f"current still has tmp {source.name}")
            continue

        if next_source is not None and not next_source.exists():
            waiting.append(f"waiting next file {next_source}")
            continue

        if next_source is None and not args.allow_last_without_next:
            waiting.append(f"waiting next file after last item {source}")
            continue

        try:
            print(f"[CROP] {source} -> {target}")
            crop_one(source, target, args)
            if args.delete_original:
                safe_delete(source)
            cropped += 1
        except subprocess.CalledProcessError as exc:
            print(f"[ERROR] crop failed: {source}: {exc}", file=sys.stderr)
            failed += 1

    return cropped, skipped, waiting, failed


def main():
    parser = argparse.ArgumentParser(
        description=(
            "裁剪 UCAR/GDEX GFS 0.25 全球 GRIB2。默认读取 data/gdex_gfs_0p25_global，"
            "只保留 31.0-31.25N, 118.50-118.75E 区域的 HGT/PRES/UGRD/VGRD 及必要层次。"
        )
    )
    parser.add_argument("--input-dir", default="./data/gdex_gfs_0p25_global")
    parser.add_argument("--output-dir", default="./data/gdex_gfs_0p25_windcheck")
    parser.add_argument("--start-cycle", default="2025110100")
    parser.add_argument("--end-cycle", default="2026022818")
    parser.add_argument("--forecast-hours", default="3,6")
    parser.add_argument("--toplat", type=float, default=31.25)
    parser.add_argument("--bottomlat", type=float, default=31.0)
    parser.add_argument("--leftlon", type=float, default=118.50)
    parser.add_argument("--rightlon", type=float, default=118.75)
    parser.add_argument("--wgrib2", default=None)
    parser.add_argument("--library-dir", default=None, help="额外加入 LD_LIBRARY_PATH 的 lib 目录，例如 /opt/miniconda3/envs/graphcast1/lib")
    parser.add_argument("--doctor", action="store_true", help="只诊断 wgrib2 动态库，不裁剪")
    parser.add_argument("--match-regex", default=None)
    parser.add_argument("--sleep-seconds", type=int, default=60, help="等待缺失文件时的重新扫描间隔，默认 60 秒")
    parser.add_argument("--once", action="store_true", help="只扫描一轮，不持续等待")
    parser.add_argument("--allow-last-without-next", action="store_true", help="最后一个文件无需等待下一个文件")
    parser.add_argument("--no-delete-original", dest="delete_original", action="store_false")
    parser.add_argument("--min-bytes", type=int, default=1024)
    parser.set_defaults(delete_original=True)
    args = parser.parse_args()

    if args.doctor:
        run_doctor(args)
        return

    start_dt = parse_utc_hour(args.start_cycle)
    end_dt = parse_utc_hour(args.end_cycle)
    forecast_hours = parse_forecast_hours(args.forecast_hours)
    items = expected_items(start_dt, end_dt, forecast_hours)

    print("=" * 100)
    print(f"Input dir: {args.input_dir}")
    print(f"Output dir: {args.output_dir}")
    print(f"Cycles UTC: {start_dt:%Y%m%d%H} -> {end_dt:%Y%m%d%H}")
    print(f"Forecast hours: {forecast_hours}")
    print(f"Region: lat={args.bottomlat}:{args.toplat}, lon={args.leftlon}:{args.rightlon}")
    print(f"Delete original after crop: {args.delete_original}")
    print(f"Match regex: {args.match_regex or build_match_regex()}")
    args.wgrib2_checked = check_wgrib2(args)

    while True:
        cropped, skipped, waiting, failed = process_once(items, args)
        print(
            f"[SUMMARY] cropped={cropped}, skipped={skipped}, "
            f"waiting={len(waiting)}, failed={failed}"
        )
        if waiting:
            print(f"[WAIT] {waiting[0]}")

        if args.once or (not waiting and failed == 0):
            break
        time.sleep(args.sleep_seconds)


if __name__ == "__main__":
    main()
