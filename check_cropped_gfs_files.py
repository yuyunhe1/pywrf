import argparse
import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_INPUT_DIR = "./data/gdex_gfs_0p25_windcheck"
DEFAULT_REPORT = "./data/gdex_gfs_0p25_windcheck_file_check.csv"
DEFAULT_START_CYCLE = "2025110100"
DEFAULT_END_CYCLE = "2026022818"
DEFAULT_FORECAST_HOURS = "3,6"


def parse_utc_hour(value):
    return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def parse_forecast_hours(value):
    hours = []
    for item in value.split(","):
        item = item.strip().lower().removeprefix("f")
        if item:
            hours.append(int(item))
    return sorted(set(hours))


def build_cycles(start_dt, end_dt):
    cur = start_dt
    while cur <= end_dt:
        yield cur
        cur += timedelta(hours=6)


def expected_items(start_dt, end_dt, forecast_hours):
    for cycle_dt in build_cycles(start_dt, end_dt):
        for forecast_hour in forecast_hours:
            yield cycle_dt, forecast_hour


def grib_path(root, cycle_dt, forecast_hour):
    ymd = cycle_dt.strftime("%Y%m%d")
    ymdh = cycle_dt.strftime("%Y%m%d%H")
    return Path(root) / ymd / f"gfs.0p25.{ymdh}.f{forecast_hour:03d}.grib2"


def parse_grib_name(path):
    match = re.search(r"gfs\.0p25\.(\d{10})\.f(\d{3})\.grib2$", path.name)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def check_files(args):
    input_dir = Path(args.input_dir)
    start_dt = parse_utc_hour(args.start_cycle)
    end_dt = parse_utc_hour(args.end_cycle)
    forecast_hours = parse_forecast_hours(args.forecast_hours)

    expected = list(expected_items(start_dt, end_dt, forecast_hours))
    expected_paths = {grib_path(input_dir, cycle_dt, fh).resolve() for cycle_dt, fh in expected}
    rows = []
    missing = []
    small = []
    ok = []

    for cycle_dt, forecast_hour in expected:
        path = grib_path(input_dir, cycle_dt, forecast_hour)
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        status = "ok"
        if not exists:
            status = "missing"
            missing.append(path)
        elif size < args.min_bytes:
            status = "too_small"
            small.append(path)
        else:
            ok.append(path)

        rows.append(
            {
                "status": status,
                "cycle_utc": cycle_dt.strftime("%Y%m%d%H"),
                "forecast_hour": forecast_hour,
                "valid_time_utc": (cycle_dt + timedelta(hours=forecast_hour)).strftime("%Y%m%d%H"),
                "path": str(path),
                "size_bytes": size,
            }
        )

    extras = []
    for path in sorted(input_dir.rglob("*.grib2")):
        resolved = path.resolve()
        if resolved in expected_paths:
            continue
        parsed = parse_grib_name(path)
        extras.append((path, parsed))

    return rows, ok, missing, small, extras


def write_report(path, rows):
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["status", "cycle_utc", "forecast_hour", "valid_time_utc", "path", "size_bytes"],
        )
        writer.writeheader()
        writer.writerows(rows)


def print_limited(title, values, limit):
    if not values:
        return
    print(title)
    for item in values[:limit]:
        print(f"  {item}")
    if len(values) > limit:
        print(f"  ... 还有 {len(values) - limit} 个未显示")


def main():
    parser = argparse.ArgumentParser(
        description="检查裁剪后的 GDEX GFS GRIB2 文件是否齐全。默认检查 2025110100 至 2026022818 的 f003/f006。"
    )
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, help="裁剪后 GRIB2 根目录")
    parser.add_argument("--start-cycle", default=DEFAULT_START_CYCLE, help="起始起报时次 UTC，格式 YYYYMMDDHH")
    parser.add_argument("--end-cycle", default=DEFAULT_END_CYCLE, help="结束起报时次 UTC，格式 YYYYMMDDHH")
    parser.add_argument("--forecast-hours", default=DEFAULT_FORECAST_HOURS, help="预报时效，例如 3,6 或 f003,f006")
    parser.add_argument("--min-bytes", type=int, default=1024, help="小于该字节数的文件视为可疑")
    parser.add_argument("--report", default=DEFAULT_REPORT, help="CSV 检查报告路径；设为空字符串则不写报告")
    parser.add_argument("--list-limit", type=int, default=80, help="终端最多显示多少条缺失/可疑/额外文件")
    args = parser.parse_args()

    rows, ok, missing, small, extras = check_files(args)
    expected_count = len(rows)

    print("=" * 100)
    print(f"Input dir: {args.input_dir}")
    print(f"Cycles UTC: {args.start_cycle} -> {args.end_cycle}")
    print(f"Forecast hours: {parse_forecast_hours(args.forecast_hours)}")
    print(f"Expected files: {expected_count}")
    print(f"OK files: {len(ok)}")
    print(f"Missing files: {len(missing)}")
    print(f"Too-small files (< {args.min_bytes} bytes): {len(small)}")
    print(f"Extra .grib2 files outside expected range/pattern: {len(extras)}")

    if args.report:
        write_report(args.report, rows)
        print(f"Report: {args.report}")

    print_limited("[MISSING]", missing, args.list_limit)
    print_limited("[TOO_SMALL]", small, args.list_limit)
    print_limited("[EXTRA]", [item[0] for item in extras], args.list_limit)

    if missing or small:
        raise SystemExit(1)
    print("[OK] 裁剪后的目标 GRIB2 文件齐全。")


if __name__ == "__main__":
    main()
