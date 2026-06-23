import argparse
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


BEIJING_TZ = timezone(timedelta(hours=8))
PRESSURE_LEVELS = [
    "1", "2", "3", "5", "7", "10", "20", "30", "50", "70", "100", "125",
    "150", "175", "200", "225", "250", "300", "350", "400", "450", "500",
    "550", "600", "650", "700", "750", "775", "800", "825", "850", "875",
    "900", "925", "950", "975", "1000",
]
PRESSURE_VARIABLES = [
    "u_component_of_wind",
    "v_component_of_wind",
    "geopotential",
]
SINGLE_LEVEL_VARIABLES = [
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "100m_u_component_of_wind",
    "100m_v_component_of_wind",
    "10m_wind_gust_since_previous_post_processing",
    "instantaneous_10m_wind_gust",
    "10m_u_component_of_neutral_wind",
    "10m_v_component_of_neutral_wind",
]


def parse_beijing_time(value):
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING_TZ)


def nearest_grid_value(value, resolution):
    return round(value / resolution) * resolution


def build_grid_area(latitude, longitude, resolution, radius):
    center_lat = nearest_grid_value(latitude, resolution)
    center_lon = nearest_grid_value(longitude, resolution)
    north = center_lat + radius * resolution
    south = center_lat - radius * resolution
    west = center_lon - radius * resolution
    east = center_lon + radius * resolution
    points = [
        {"latitude": round(lat, 6), "longitude": round(lon, 6)}
        for lat_index in range(radius, -radius - 1, -1)
        for lon_index in range(-radius, radius + 1)
        for lat, lon in [
            (center_lat + lat_index * resolution, center_lon + lon_index * resolution)
        ]
    ]
    return [north, west, south, east], points, center_lat, center_lon


def iter_month_windows(start_utc, end_utc):
    cursor = start_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor <= end_utc:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1)
        month_end = next_month - timedelta(hours=1)
        yield max(start_utc, cursor), min(end_utc, month_end)
        cursor = next_month


def request_dates_and_times(window_start, window_end):
    days = []
    times = []
    cursor = window_start
    while cursor <= window_end:
        day = f"{cursor.day:02d}"
        hour = f"{cursor.hour:02d}:00"
        if day not in days:
            days.append(day)
        if hour not in times:
            times.append(hour)
        cursor += timedelta(hours=1)
    return days, times


def is_valid_netcdf(path, min_bytes):
    path = Path(path)
    return path.exists() and path.stat().st_size >= min_bytes


def retrieve_with_retry(client, dataset, request, target, retries, min_bytes):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")

    if is_valid_netcdf(target, min_bytes):
        print(f"[SKIP] {target}")
        return True

    for attempt in range(1, retries + 1):
        try:
            if partial.exists():
                partial.unlink()
            print(f"[REQUEST] {dataset} -> {target.name}")
            client.retrieve(dataset, request, str(partial))
            if not is_valid_netcdf(partial, min_bytes):
                raise RuntimeError(f"下载文件过小: {partial.stat().st_size if partial.exists() else 0} bytes")
            partial.replace(target)
            print(f"[DONE] {target} ({target.stat().st_size / 1024 / 1024:.2f} MB)")
            return True
        except Exception as exc:
            print(f"[ERROR] attempt {attempt}/{retries}: {exc}")
            if partial.exists():
                partial.unlink()
            if attempt < retries:
                time.sleep(60 * attempt)
    return False


def validate_cds_config():
    config_path = Path(os.environ.get("CDSAPI_RC", Path.home() / ".cdsapirc")).expanduser()
    if not config_path.exists():
        raise RuntimeError(
            f"未找到 CDS API 配置文件: {config_path}\n"
            "请登录 https://cds.climate.copernicus.eu/profile 获取个人访问令牌，然后创建该文件:\n"
            "  url: https://cds.climate.copernicus.eu/api\n"
            "  key: <PERSONAL-ACCESS-TOKEN>\n"
            "创建后建议执行: chmod 600 ~/.cdsapirc"
        )

    text = config_path.read_text(encoding="utf-8", errors="ignore")
    if "url:" not in text or "key:" not in text:
        raise RuntimeError(
            f"CDS API 配置文件不完整: {config_path}\n"
            "文件应至少包含:\n"
            "  url: https://cds.climate.copernicus.eu/api\n"
            "  key: <PERSONAL-ACCESS-TOKEN>"
        )
    return config_path


def build_common_request(window_start, window_end, area):
    days, times = request_dates_and_times(window_start, window_end)
    return {
        "product_type": ["reanalysis"],
        "year": [str(window_start.year)],
        "month": [f"{window_start.month:02d}"],
        "day": days,
        "time": times,
        "area": area,
        "data_format": "netcdf",
        "download_format": "unarchived",
    }


def main():
    parser = argparse.ArgumentParser(
        description="下载目标点周围 ERA5 小时风场：37 个等压层风场、位势，以及单层风和阵风。"
    )
    parser.add_argument("--start-bjt", default="2025-11-01 00:00", help="北京时间，格式 YYYY-MM-DD HH:MM")
    parser.add_argument("--end-bjt", default="2026-02-28 23:00", help="北京时间，格式 YYYY-MM-DD HH:MM")
    parser.add_argument("--latitude", type=float, default=31.12)
    parser.add_argument("--longitude", type=float, default=118.66)
    parser.add_argument("--resolution", type=float, default=0.25)
    parser.add_argument("--grid-radius", type=int, default=1, help="中心点四周网格半径；1 表示下载 3x3 网格")
    parser.add_argument("--output-dir", default="./data/ERA5_Wind_202511_202602")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--min-bytes", type=int, default=1024)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start_bjt = parse_beijing_time(args.start_bjt)
    end_bjt = parse_beijing_time(args.end_bjt)
    if end_bjt < start_bjt:
        raise ValueError("结束时间不能早于开始时间")

    start_utc = start_bjt.astimezone(timezone.utc)
    end_utc = end_bjt.astimezone(timezone.utc)
    area, points, center_lat, center_lon = build_grid_area(
        args.latitude, args.longitude, args.resolution, args.grid_radius
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "requested_point": {"latitude": args.latitude, "longitude": args.longitude},
        "nearest_center_grid": {"latitude": center_lat, "longitude": center_lon},
        "grid_resolution_degrees": args.resolution,
        "cds_area_north_west_south_east": area,
        "grid_points": points,
        "start_beijing": start_bjt.isoformat(),
        "end_beijing": end_bjt.isoformat(),
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "pressure_levels_hpa": PRESSURE_LEVELS,
        "pressure_variables": PRESSURE_VARIABLES,
        "single_level_variables": SINGLE_LEVEL_VARIABLES,
        "note": "Pressure-level wind speed can be calculated as sqrt(u**2 + v**2).",
    }
    (output_dir / "download_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"北京时间: {start_bjt} -> {end_bjt}")
    print(f"UTC 时间: {start_utc} -> {end_utc}")
    print(f"最近中心网格: ({center_lon:.2f}E, {center_lat:.2f}N)")
    print(f"CDS area [N, W, S, E]: {area}")
    print(f"网格点数量: {len(points)}")

    if args.dry_run:
        client = None
    else:
        config_path = validate_cds_config()
        print(f"使用 CDS API 配置: {config_path}")
        try:
            import cdsapi
        except ImportError as exc:
            raise RuntimeError('缺少 cdsapi，请执行: pip install "cdsapi>=0.7.7"') from exc
        try:
            client = cdsapi.Client(progress=True, quiet=False)
        except Exception as exc:
            raise RuntimeError(
                f"CDS API 客户端初始化失败，请检查 {config_path} 中的 url 和 key。\n"
                "还需要登录 CDS，在 ERA5 pressure-levels 和 single-levels 数据集页面接受许可条款。"
            ) from exc
    failures = []

    for window_start, window_end in iter_month_windows(start_utc, end_utc):
        common = build_common_request(window_start, window_end, area)
        month_tag = window_start.strftime("%Y%m")

        pressure_request = {
            **common,
            "variable": PRESSURE_VARIABLES,
            "pressure_level": PRESSURE_LEVELS,
        }
        single_request = {
            **common,
            "variable": SINGLE_LEVEL_VARIABLES,
        }
        requests = [
            (
                "reanalysis-era5-pressure-levels",
                pressure_request,
                output_dir / f"era5_wind_pressure_levels_{month_tag}.nc",
            ),
            (
                "reanalysis-era5-single-levels",
                single_request,
                output_dir / f"era5_wind_single_levels_{month_tag}.nc",
            ),
        ]

        for dataset, request, target in requests:
            if args.dry_run:
                print(f"\n[DRY RUN] {dataset} -> {target}")
                print(json.dumps(request, indent=2))
                continue
            if not retrieve_with_retry(client, dataset, request, target, args.retries, args.min_bytes):
                failures.append(str(target))

    if failures:
        raise RuntimeError(f"以下文件下载失败: {failures}")
    print(f"完成。数据目录: {output_dir}")


if __name__ == "__main__":
    main()
