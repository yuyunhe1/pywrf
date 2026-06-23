import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from crop_gdex_gfs_point import check_wgrib2, crop_one, safe_delete
from download_gfs_hourly_70vars_realtime import GDEX_BASE_URL, build_gdex_url, download


DEFAULT_MISSING_TARGETS = [
    "data/gdex_gfs_0p25_windcheck/20260123/gfs.0p25.2026012306.f003.grib2",
    "data/gdex_gfs_0p25_windcheck/20260123/gfs.0p25.2026012306.f006.grib2",
    "data/gdex_gfs_0p25_windcheck/20260214/gfs.0p25.2026021400.f003.grib2",
    "data/gdex_gfs_0p25_windcheck/20260214/gfs.0p25.2026021406.f003.grib2",
    "data/gdex_gfs_0p25_windcheck/20260228/gfs.0p25.2026022806.f003.grib2",
    "data/gdex_gfs_0p25_windcheck/20260228/gfs.0p25.2026022806.f006.grib2",
    "data/gdex_gfs_0p25_windcheck/20260228/gfs.0p25.2026022818.f003.grib2",
    "data/gdex_gfs_0p25_windcheck/20260228/gfs.0p25.2026022818.f006.grib2",
]


def parse_missing_target(path):
    path = Path(path)
    match = re.search(r"gfs\.0p25\.(\d{10})\.f(\d{3})\.grib2$", path.name)
    if not match:
        raise ValueError(f"无法从文件名解析 cycle/forecast_hour: {path}")
    cycle_dt = datetime.strptime(match.group(1), "%Y%m%d%H").replace(tzinfo=timezone.utc)
    forecast_hour = int(match.group(2))
    return path, cycle_dt, forecast_hour


def source_path(raw_dir, cycle_dt, forecast_hour):
    ymd = cycle_dt.strftime("%Y%m%d")
    ymdh = cycle_dt.strftime("%Y%m%d%H")
    return Path(raw_dir) / ymd / f"gfs.0p25.{ymdh}.f{forecast_hour:03d}.grib2"


def meta_path(path):
    path = Path(path)
    return path.with_suffix(path.suffix + ".meta.json")


def write_source_meta(path, cycle_dt, forecast_hour, args):
    meta = {
        "source": "UCAR GDEX d084001 NCEP GFS 0.25 Degree Global Forecast Grids",
        "url": build_gdex_url(cycle_dt, forecast_hour, args.gdex_base_url),
        "cycle_utc": cycle_dt.strftime("%Y%m%d%H"),
        "forecast_hour": forecast_hour,
        "valid_time_utc": (cycle_dt + args.forecast_delta(forecast_hour)).strftime("%Y%m%d%H"),
        "grid": "0.25 degree global 1440x721",
        "downloaded_by": Path(__file__).name,
        "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    meta_path(path).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def is_valid_file(path, min_bytes):
    path = Path(path)
    return path.exists() and path.stat().st_size >= min_bytes


def cleanup_crop_temps(target):
    target = Path(target)
    for path in [
        target.with_suffix(target.suffix + ".vars.tmp"),
        target.with_suffix(target.suffix + ".tmp"),
    ]:
        if path.exists():
            path.unlink()


def read_targets(args):
    values = []
    if args.missing_list:
        for line in Path(args.missing_list).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                values.append(line)
    values.extend(args.targets)
    if not values:
        values = DEFAULT_MISSING_TARGETS

    unique = []
    seen = set()
    for value in values:
        path = Path(value)
        key = path.as_posix()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def process_target(target, args):
    target, cycle_dt, forecast_hour = parse_missing_target(target)
    raw = source_path(args.raw_dir, cycle_dt, forecast_hour)
    url = build_gdex_url(cycle_dt, forecast_hour, args.gdex_base_url)

    if is_valid_file(target, args.cropped_min_bytes) and not args.force:
        print(f"[SKIP] cropped exists: {target}")
        if args.delete_original and raw.exists():
            safe_delete(raw)
        return "skipped"

    if args.print_url_only:
        print(f"[TARGET] {target}")
        print(f"[SOURCE] {raw}")
        print(url)
        return "printed"

    for attempt in range(1, args.crop_retries + 1):
        redownload_after_failure = attempt > 1 and args.redownload_on_crop_fail
        need_download = args.force_download or redownload_after_failure or not is_valid_file(raw, args.raw_min_bytes)

        if need_download:
            if raw.exists():
                print(f"[RETRY] remove bad raw before download: {raw}")
                safe_delete(raw)
            ok = download(url, raw, retries=args.retries, timeout=args.timeout, min_bytes=args.raw_min_bytes)
            if not ok:
                print(f"[ERROR] download failed: {raw}", file=sys.stderr)
                return "failed"
            write_source_meta(raw, cycle_dt, forecast_hour, args)
        else:
            print(f"[SKIP] raw exists: {raw}")
            if not meta_path(raw).exists():
                write_source_meta(raw, cycle_dt, forecast_hour, args)

        print(f"[CROP] attempt {attempt}/{args.crop_retries}: {raw} -> {target}")
        try:
            crop_one(raw, target, args)
            if not is_valid_file(target, args.cropped_min_bytes):
                raise RuntimeError(f"cropped file too small or missing: {target}")
        except Exception as exc:
            cleanup_crop_temps(target)
            print(f"[ERROR] crop failed: {raw}: {exc}", file=sys.stderr)
            if attempt < args.crop_retries and args.redownload_on_crop_fail:
                print(f"[RETRY] crop failed, will redownload raw and crop again: {raw}")
                safe_delete(raw)
                continue
            return "failed"

        if args.delete_original:
            safe_delete(raw)
        print(f"[DONE] {target} ({target.stat().st_size / 1024:.1f} KiB)")
        return "done"

    return "failed"


def forecast_delta(hours):
    from datetime import timedelta

    return timedelta(hours=hours)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "下载缺失的 UCAR/GDEX GFS 全球 GRIB2，并按 windcheck 需求裁剪到 "
            "31.0-31.25N, 118.50-118.75E。默认处理脚本内置的 8 个缺失文件。"
        )
    )
    parser.add_argument("targets", nargs="*", help="缺失的裁剪目标 .grib2 路径；不填则使用脚本内置列表")
    parser.add_argument("--missing-list", default=None, help="包含缺失目标路径的文本文件，每行一个")
    parser.add_argument("--raw-dir", default="./data/gdex_gfs_0p25_global", help="原始全球 GRIB2 下载目录")
    parser.add_argument("--gdex-base-url", default=GDEX_BASE_URL)
    parser.add_argument("--wgrib2", default=None)
    parser.add_argument("--library-dir", default=None, help="额外加入 LD_LIBRARY_PATH 的 lib 目录")
    parser.add_argument("--toplat", type=float, default=31.25)
    parser.add_argument("--bottomlat", type=float, default=31.0)
    parser.add_argument("--leftlon", type=float, default=118.50)
    parser.add_argument("--rightlon", type=float, default=118.75)
    parser.add_argument("--match-regex", default=None)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--raw-min-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--cropped-min-bytes", type=int, default=1024)
    parser.add_argument("--crop-retries", type=int, default=2, help="裁剪失败后的总尝试次数；默认 2，即失败后重下再裁剪一次")
    parser.add_argument(
        "--no-redownload-on-crop-fail",
        dest="redownload_on_crop_fail",
        action="store_false",
        help="裁剪失败时不自动删除原始文件并重新下载",
    )
    parser.add_argument("--force", action="store_true", help="即使裁剪目标已存在也重新裁剪")
    parser.add_argument("--force-download", action="store_true", help="即使原始全球文件已存在也重新下载")
    parser.add_argument("--print-url-only", action="store_true", help="只打印目标、原始路径和 URL，不下载/裁剪")
    parser.add_argument("--no-delete-original", dest="delete_original", action="store_false")
    parser.set_defaults(delete_original=True, redownload_on_crop_fail=True)
    args = parser.parse_args()
    args.forecast_delta = forecast_delta

    targets = read_targets(args)
    print("=" * 100)
    print(f"Targets: {len(targets)}")
    print(f"Raw dir: {args.raw_dir}")
    print(f"Region: lat={args.bottomlat}:{args.toplat}, lon={args.leftlon}:{args.rightlon}")
    print(f"Delete original after crop: {args.delete_original}")

    if not args.print_url_only:
        args.wgrib2_checked = check_wgrib2(args)
    else:
        args.wgrib2_checked = args.wgrib2

    counts = {"done": 0, "skipped": 0, "printed": 0, "failed": 0}
    for target in targets:
        result = process_target(target, args)
        counts[result] = counts.get(result, 0) + 1

    print("=" * 100)
    print(
        f"Summary: done={counts.get('done', 0)}, skipped={counts.get('skipped', 0)}, "
        f"printed={counts.get('printed', 0)}, failed={counts.get('failed', 0)}"
    )
    if counts.get("failed", 0):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
