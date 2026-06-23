import argparse
import contextlib
import csv
import io
import math
import os
import re
import shutil
import subprocess
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


warnings.filterwarnings("ignore", message="Workbook contains no default style")

BEIJING_TZ = timezone(timedelta(hours=8))
LOW_LEVEL_HEIGHTS_M = [10, 20, 30, 40, 50, 80, 100]
GFS_CYCLE_HOURS = [0, 6, 12, 18]


def parse_utc_hour(value):
    return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def beijing_day_window(date_text):
    start_bj = datetime.strptime(date_text, "%Y%m%d").replace(tzinfo=BEIJING_TZ)
    end_bj = start_bj + timedelta(days=1)
    return start_bj, end_bj, start_bj.astimezone(timezone.utc), end_bj.astimezone(timezone.utc)


def nearby_dates(date_text):
    day = datetime.strptime(date_text, "%Y%m%d")
    return [(day + timedelta(days=offset)).strftime("%Y%m%d") for offset in (-1, 0, 1)]


def observation_format_for_date(date_text, obs_format="auto"):
    if obs_format != "auto":
        return obs_format
    month = int(date_text[4:6])
    if 7 <= month <= 10:
        return "new"
    if month in {11, 12, 1, 2}:
        return "old"
    return "new"


def observation_search_roots(obs_dir, date_text, file_format):
    root = Path(obs_dir)
    month_dir = datetime.strptime(date_text, "%Y%m%d").strftime("%Y-%m")
    if file_format == "old":
        roots = [root] if root.name == "202511~202602" else [root / "202511~202602"]
    else:
        roots = [root] if root.name == month_dir else [root / month_dir]
    return list(dict.fromkeys(roots))


def observation_patterns(date_text, file_format):
    day = datetime.strptime(date_text, "%Y%m%d")
    dashed = day.strftime("%Y-%m-%d")
    if file_format == "old":
        return [
            f"WindData_product_*_{date_text}_10min.xlsx",
            f"WindData_product_*_{date_text}_10min.xls",
        ]
    return [
        f"*{dashed}*.xls",
        f"*{dashed}*.xlsx",
        f"*{date_text}*.xls",
        f"*{date_text}*.xlsx",
    ]


def find_observation_files(obs_dir, compare_date, obs_format="auto"):
    paths = []
    for date_text in nearby_dates(compare_date):
        file_format = observation_format_for_date(date_text, obs_format)
        patterns = observation_patterns(date_text, file_format)
        for root in observation_search_roots(obs_dir, date_text, file_format):
            if not root.exists():
                continue
            for pattern in patterns:
                paths.extend((path, file_format) for path in root.glob(pattern))

    unique = []
    seen = set()
    for path, file_format in sorted(paths, key=lambda item: str(item[0])):
        if path.name.startswith("~$"):
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append((path, file_format))
    return unique


def find_observation_files_for_range(obs_dir, start_date, end_date, obs_format="auto", include_margin_days=1):
    start = datetime.strptime(start_date, "%Y%m%d") - timedelta(days=include_margin_days)
    end = datetime.strptime(end_date, "%Y%m%d") + timedelta(days=include_margin_days)
    paths = []
    cur = start
    while cur <= end:
        date_text = cur.strftime("%Y%m%d")
        file_format = observation_format_for_date(date_text, obs_format)
        patterns = observation_patterns(date_text, file_format)
        for root in observation_search_roots(obs_dir, date_text, file_format):
            if not root.exists():
                continue
            for pattern in patterns:
                paths.extend((path, file_format) for path in root.glob(pattern))
        cur += timedelta(days=1)

    unique = []
    seen = set()
    for path, file_format in sorted(paths, key=lambda item: str(item[0])):
        if path.name.startswith("~$"):
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append((path, file_format))
    return unique


def meta_path(path):
    path = Path(path)
    return path.with_suffix(path.suffix + ".meta.json")


def find_wgrib2(path):
    if path:
        return path
    return shutil.which("wgrib2")


def build_subprocess_env(args):
    env = os.environ.copy()
    lib_dirs = []
    if getattr(args, "library_dir", None):
        lib_dirs.append(args.library_dir)
    if env.get("CONDA_PREFIX"):
        lib_dirs.append(str(Path(env["CONDA_PREFIX"]) / "lib"))
    if getattr(args, "wgrib2", None):
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


def infer_valid_time_from_path(path):
    match = re.search(r"gfs\.0p25\.(\d{10})\.f(\d{3})\.grib2$", Path(path).name)
    if not match:
        return None
    cycle_dt = parse_utc_hour(match.group(1))
    return cycle_dt + timedelta(hours=int(match.group(2)))


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


def read_observation_excel(path):
    return pd.read_excel(path, header=None)


def header_index(headers, include, exclude=None):
    exclude = exclude or []
    for idx, value in enumerate(headers):
        text = str(value).strip()
        if any(item in text for item in include) and not any(item in text for item in exclude):
            return idx
    return None


def parse_long_observation_table(raw):
    headers = raw.iloc[0].tolist()
    time_col = header_index(headers, ["记录时间", "时间"])
    speed_col = header_index(headers, ["风速"], exclude=["垂直"])
    dir_col = header_index(headers, ["风向", "角度"])
    height_col = header_index(headers, ["高度"])
    if time_col is None or speed_col is None or height_col is None:
        return None

    data = raw.iloc[1:].copy()
    times_bj = pd.to_datetime(data.iloc[:, time_col], errors="coerce")
    speed = pd.to_numeric(data.iloc[:, speed_col], errors="coerce").replace(-999, np.nan)
    height = pd.to_numeric(data.iloc[:, height_col], errors="coerce").replace(-999, np.nan)
    direction = pd.Series(np.nan, index=data.index, dtype=float)
    if dir_col is not None:
        direction = pd.to_numeric(data.iloc[:, dir_col], errors="coerce").replace(-999, np.nan)

    return pd.DataFrame(
        {
            "time_bj": times_bj,
            "time_utc": times_bj.dt.tz_localize(BEIJING_TZ).dt.tz_convert(timezone.utc),
            "height_m": height.astype(float),
            "obs_speed": speed.astype(float),
            "obs_dir": direction.astype(float),
        }
    ).dropna(subset=["time_utc", "height_m", "obs_speed"])


def parse_wide_observation_table(raw):
    heights = raw.iloc[0, 1:].to_numpy()
    data = raw.iloc[1:].copy()
    times_bj = pd.to_datetime(data.iloc[:, 0], errors="coerce")
    rows = []

    for col in range(1, raw.shape[1], 3):
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
    return pd.concat(rows, ignore_index=True)


def load_observations(obs_dir, compare_date, obs_format="auto"):
    rows = []
    paths = find_observation_files(obs_dir, compare_date, obs_format)

    for path, file_format in paths:
        print(f"[OBS:{file_format}] {path}")
        raw = read_observation_excel(path)
        if file_format == "old":
            obs_part = parse_wide_observation_table(raw)
        else:
            obs_part = parse_long_observation_table(raw)
            if obs_part is None:
                obs_part = parse_wide_observation_table(raw)
        rows.append(obs_part)

    if not rows:
        return pd.DataFrame(columns=["time_bj", "time_utc", "height_m", "obs_speed", "obs_dir"])

    obs = pd.concat(rows, ignore_index=True)
    obs["valid_time_utc"] = obs["time_utc"].dt.floor("h")
    return obs


def load_observations_for_range(obs_dir, start_date, end_date, obs_format="auto"):
    rows = []
    paths = find_observation_files_for_range(obs_dir, start_date, end_date, obs_format)

    for path, file_format in paths:
        print(f"[OBS:{file_format}] {path}")
        raw = read_observation_excel(path)
        if file_format == "old":
            obs_part = parse_wide_observation_table(raw)
        else:
            obs_part = parse_long_observation_table(raw)
            if obs_part is None:
                obs_part = parse_wide_observation_table(raw)
        rows.append(obs_part)

    if not rows:
        return pd.DataFrame(columns=["time_bj", "time_utc", "height_m", "obs_speed", "obs_dir"])

    obs = pd.concat(rows, ignore_index=True)
    obs["valid_time_utc"] = obs["time_utc"].dt.floor("h")
    return obs


def aggregate_observations(obs, mode, target_times_utc, window_hours):
    if mode == "previous_3h_mean":
        rows = []
        for target_time in target_times_utc:
            target_ts = pd.Timestamp(target_time)
            start_ts = target_ts - pd.Timedelta(hours=window_hours)
            window = obs[(obs["time_utc"] > start_ts) & (obs["time_utc"] <= target_ts)].copy()
            if window.empty:
                continue
            speed_part = (
                window.groupby("height_m", as_index=False)
                .agg(obs_speed=("obs_speed", "mean"), sample_count=("obs_speed", "count"))
            )
            dir_part = (
                window.groupby("height_m")["obs_dir"]
                .apply(circular_mean_deg)
                .reset_index(name="obs_dir")
            )
            part = speed_part.merge(dir_part, on="height_m", how="left")
            part["valid_time_utc"] = target_ts
            part["time_bj"] = target_ts.tz_convert(BEIJING_TZ)
            part["obs_window_start_utc"] = start_ts
            part["obs_window_end_utc"] = target_ts
            rows.append(part)
        if not rows:
            return pd.DataFrame(
                columns=[
                    "valid_time_utc",
                    "height_m",
                    "obs_speed",
                    "sample_count",
                    "obs_dir",
                    "time_bj",
                    "obs_window_start_utc",
                    "obs_window_end_utc",
                ]
            )
        return pd.concat(rows, ignore_index=True)

    obs = obs[obs["valid_time_utc"].isin(target_times_utc)].copy()
    if mode == "hourly_mean":
        obs_match = (
            obs.groupby(["valid_time_utc", "height_m"], as_index=False)
            .agg(obs_speed=("obs_speed", "mean"), time_bj=("time_bj", "max"))
        )
        obs_dir = (
            obs.groupby(["valid_time_utc", "height_m"])["obs_dir"]
            .apply(circular_mean_deg)
            .reset_index(name="obs_dir")
        )
        return obs_match.merge(obs_dir, on=["valid_time_utc", "height_m"], how="left")

    return obs[obs["time_bj"].dt.minute == 0].copy()


def open_grib_dataset(path, filter_by_keys=None):
    try:
        import xarray as xr
        import cfgrib  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "读取 GRIB2 需要 cfgrib/eccodes：conda install -c conda-forge cfgrib eccodes"
        ) from exc
    return xr.open_dataset(path, engine="cfgrib", backend_kwargs={"filter_by_keys": filter_by_keys or {}})


def try_open_grib_dataset(path, filter_by_keys=None):
    try:
        return open_grib_dataset(path, filter_by_keys)
    except Exception as exc:
        print(f"[WARN] 跳过 GRIB 分组 {filter_by_keys}: {exc}")
        return None


def try_open_grib_dataset_quiet(path, filter_by_keys=None):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return open_grib_dataset(path, filter_by_keys)
    except Exception:
        return None


def pick_var(ds, candidates):
    for name in candidates:
        if name in ds:
            return ds[name]
    raise KeyError(f"GRIB 中缺少变量，候选名: {candidates}; 实际变量: {list(ds.data_vars)}")


def grid_value(da, lat, lon, spatial_aggregation):
    if spatial_aggregation == "nearest":
        selector = {}
        if "latitude" in da.coords:
            selector["latitude"] = lat
        if "longitude" in da.coords:
            selector["longitude"] = lon % 360
        if selector:
            da = da.sel(selector, method="nearest")
    else:
        dims = [dim for dim in ("latitude", "longitude") if dim in da.dims]
        if dims:
            da = da.mean(dim=dims, skipna=True)
    arr = np.asarray(da.squeeze())
    return float(arr.reshape(-1)[0])


def read_wgrib2_csv_values(path, variable, level, args):
    wgrib2 = find_wgrib2(args.wgrib2)
    if not wgrib2:
        return None

    regex = rf":{variable}:{level}:"
    result = subprocess.run(
        [wgrib2, str(path), "-match", regex, "-csv", "-"],
        capture_output=True,
        text=True,
        env=build_subprocess_env(args),
    )
    if result.returncode != 0:
        return None

    rows = []
    reader = csv.reader(io.StringIO(result.stdout))
    for row in reader:
        if len(row) < 7:
            continue
        try:
            rows.append(
                {
                    "lon": float(row[-3]),
                    "lat": float(row[-2]),
                    "value": float(row[-1]),
                }
            )
        except ValueError:
            continue
    return rows or None


def aggregate_wgrib2_rows(rows, lat, lon, spatial_aggregation):
    if not rows:
        return np.nan
    if spatial_aggregation == "nearest":
        lon_value = lon % 360
        nearest = min(rows, key=lambda row: (row["lat"] - lat) ** 2 + (row["lon"] - lon_value) ** 2)
        return float(nearest["value"])
    return float(np.mean([row["value"] for row in rows]))


def read_low_level_wind_with_wgrib2(path, height, lat, lon, spatial_aggregation, args):
    level = f"{int(height)} m above ground"
    u_rows = read_wgrib2_csv_values(path, "UGRD", level, args)
    v_rows = read_wgrib2_csv_values(path, "VGRD", level, args)
    if not u_rows or not v_rows:
        return None
    return (
        aggregate_wgrib2_rows(u_rows, lat, lon, spatial_aggregation),
        aggregate_wgrib2_rows(v_rows, lat, lon, spatial_aggregation),
    )


def coord_name(da, candidates):
    for name in candidates:
        if name in da.coords:
            return name
    return None


def first_coord_name(ds, candidates):
    for name in candidates:
        if name in ds.coords:
            return name
    return None


def pick_first_data_var(ds):
    for name in ds.data_vars:
        return ds[name]
    raise KeyError("GRIB Dataset 中没有 data_vars")


def read_height_above_ground_var(path, height, var_kind):
    if var_kind == "u":
        short_names = [f"{int(height)}u", f"u{int(height)}", "u", "ugrd"]
        candidates = [f"u{int(height)}", "u", "ugrd"]
    else:
        short_names = [f"{int(height)}v", f"v{int(height)}", "v", "vgrd"]
        candidates = [f"v{int(height)}", "v", "vgrd"]
    for short_name in short_names:
        filters_to_try = [
            {"typeOfLevel": "heightAboveGround", "level": height, "shortName": short_name},
            {"typeOfLevel": "heightAboveGround", "scaledValueOfFirstFixedSurface": int(height), "shortName": short_name},
        ]
        for filters in filters_to_try:
            ds = try_open_grib_dataset_quiet(path, filters)
            if ds is not None and ds.data_vars:
                try:
                    return pick_var(ds, candidates)
                except KeyError:
                    return pick_first_data_var(ds)
    for filters in [
        {"typeOfLevel": "heightAboveGround", "level": height},
        {"typeOfLevel": "heightAboveGround", "scaledValueOfFirstFixedSurface": int(height)},
    ]:
        ds = try_open_grib_dataset_quiet(path, filters)
        if ds is not None and ds.data_vars:
            try:
                return pick_var(ds, candidates)
            except KeyError:
                pass
    return None


def read_pressure_from_ground_layer_var(path, var_kind):
    short_names = {
        "u": ["u", "ugrd", "unknown"],
        "v": ["v", "vgrd", "unknown"],
    }[var_kind]
    candidates = {"u": ["u", "ugrd"], "v": ["v", "vgrd"]}[var_kind]
    level_types = [
        "heightAboveGroundLayer",
        "pressureFromGroundLayer",
        "heightAboveGround",
        "unknown",
    ]
    for type_of_level in level_types:
        for short_name in short_names:
            filters_to_try = [
                {"typeOfLevel": type_of_level, "shortName": short_name},
                {"typeOfLevel": type_of_level, "topLevel": 0, "bottomLevel": 30, "shortName": short_name},
                {"typeOfLevel": type_of_level, "scaledValueOfFirstFixedSurface": 30, "shortName": short_name},
                {"typeOfLevel": type_of_level, "scaledValueOfSecondFixedSurface": 0, "shortName": short_name},
            ]
            for filters in filters_to_try:
                ds = try_open_grib_dataset_quiet(path, filters)
                if ds is not None and ds.data_vars:
                    try:
                        return pick_var(ds, candidates)
                    except KeyError:
                        if short_name != "unknown":
                            return pick_first_data_var(ds)
    return None


def estimate_layer_height_from_pressure(profile, surface_pressure_hpa, layer_top_mb=30.0):
    pressure = profile["pressure_hpa"].to_numpy(dtype=float)
    height = profile["height_agl_m"].to_numpy(dtype=float)
    good = np.isfinite(pressure) & np.isfinite(height)
    if good.sum() < 2 or not np.isfinite(surface_pressure_hpa):
        return np.nan

    target_pressure = surface_pressure_hpa - layer_top_mb / 2.0
    order = np.argsort(pressure[good])
    p_sorted = pressure[good][order]
    h_sorted = height[good][order]
    return float(np.interp(target_pressure, p_sorted, h_sorted, left=np.nan, right=np.nan))


def read_gfs_profile(path, lat, lon, spatial_aggregation, args):
    ds_iso = open_grib_dataset(path, {"typeOfLevel": "isobaricInhPa"})
    ds_surface = open_grib_dataset(path, {"typeOfLevel": "surface"})

    gh = pick_var(ds_iso, ["gh", "hgt"])
    u = pick_var(ds_iso, ["u", "ugrd"])
    v = pick_var(ds_iso, ["v", "vgrd"])
    terrain = pick_var(ds_surface, ["orog", "gh", "hgt"])
    surface_pressure_hpa = np.nan
    try:
        surface_pressure = pick_var(ds_surface, ["sp", "pres"])
        surface_pressure_hpa = grid_value(surface_pressure, lat, lon, spatial_aggregation) / 100.0
    except Exception:
        pass

    pressure_coord = "isobaricInhPa" if "isobaricInhPa" in gh.coords else "level"
    terrain_m = grid_value(terrain, lat, lon, spatial_aggregation)
    records = []
    for pressure in np.asarray(gh[pressure_coord].values, dtype=float):
        one = {pressure_coord: pressure}
        h_msl = grid_value(gh.sel(one), lat, lon, spatial_aggregation)
        uu = grid_value(u.sel(one), lat, lon, spatial_aggregation)
        vv = grid_value(v.sel(one), lat, lon, spatial_aggregation)
        records.append(
            {
                "pressure_hpa": pressure,
                "height_agl_m": h_msl - terrain_m,
                "u": uu,
                "v": vv,
                "source_level": f"{pressure:g} hPa",
            }
        )

    for height in LOW_LEVEL_HEIGHTS_M:
        wind = None
        if args.low_level_reader in {"auto", "wgrib2"}:
            wind = read_low_level_wind_with_wgrib2(path, height, lat, lon, spatial_aggregation, args)

        if wind is None and args.low_level_reader in {"auto", "cfgrib"}:
            u_hag = read_height_above_ground_var(path, height, "u")
            v_hag = read_height_above_ground_var(path, height, "v")
            if u_hag is not None and v_hag is not None:
                wind = (
                    grid_value(u_hag, lat, lon, spatial_aggregation),
                    grid_value(v_hag, lat, lon, spatial_aggregation),
                )

        if wind is None:
            print(f"[WARN] 未读取到 {height} m above ground 的 U/V，跳过该低空层")
            continue
        uu, vv = wind
        records.append(
            {
                "pressure_hpa": np.nan,
                "height_agl_m": float(height),
                "u": uu,
                "v": vv,
                "source_level": f"{height:g} m AGL",
            }
        )

    profile = pd.DataFrame(records).sort_values("height_agl_m")
    if getattr(args, "include_pressure_ground_layer", False):
        u_layer = read_pressure_from_ground_layer_var(path, "u")
        v_layer = read_pressure_from_ground_layer_var(path, "v")
        layer_height = estimate_layer_height_from_pressure(profile, surface_pressure_hpa, layer_top_mb=30.0)
        if u_layer is not None and v_layer is not None and np.isfinite(layer_height):
            uu = grid_value(u_layer, lat, lon, spatial_aggregation)
            vv = grid_value(v_layer, lat, lon, spatial_aggregation)
            profile = pd.concat(
                [
                    profile,
                    pd.DataFrame(
                        [
                            {
                                "pressure_hpa": surface_pressure_hpa - 15.0,
                                "height_agl_m": layer_height,
                                "u": uu,
                                "v": vv,
                                "source_level": "30-0 mb above ground",
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )
        else:
            print("[WARN] 未读取到可用的 30-0 mb above ground U/V 或无法估算其高度，跳过该层")

    profile = profile.sort_values("height_agl_m")
    valid_time = pd.Timestamp(ds_iso.valid_time.values).tz_localize(timezone.utc)
    return valid_time, profile


def mean_duplicate_heights(profile):
    numeric = profile[["height_agl_m", "pressure_hpa", "u", "v"]].copy()
    numeric["source_level"] = profile["source_level"].astype(str)
    grouped = (
        numeric.groupby("height_agl_m", as_index=False)
        .agg(
            pressure_hpa=("pressure_hpa", "mean"),
            u=("u", "mean"),
            v=("v", "mean"),
            source_level=("source_level", lambda values: ";".join(sorted(set(values)))),
        )
    )
    return grouped


def interpolate_or_nearest(height, x, values):
    if len(x) == 0:
        return np.nan, "missing", np.nan, np.nan
    exact = np.where(np.isclose(x, height, atol=0.01))[0]
    if len(exact):
        idx = int(exact[0])
        return float(values[idx]), "exact", float(x[idx]), float(x[idx])
    if len(x) == 1 or height <= x[0] or height >= x[-1]:
        idx = int(np.argmin(np.abs(x - height)))
        return float(values[idx]), "nearest", float(x[idx]), float(x[idx])

    upper_idx = int(np.searchsorted(x, height, side="right"))
    lower_idx = upper_idx - 1
    lower_h = float(x[lower_idx])
    upper_h = float(x[upper_idx])
    weight = (height - lower_h) / (upper_h - lower_h)
    value = float(values[lower_idx] + weight * (values[upper_idx] - values[lower_idx]))
    return value, "linear", lower_h, upper_h


def interpolate_profile(profile, obs_heights):
    profile = mean_duplicate_heights(profile)
    x = profile["height_agl_m"].to_numpy(dtype=float)
    u = profile["u"].to_numpy(dtype=float)
    v = profile["v"].to_numpy(dtype=float)
    pressure = profile["pressure_hpa"].to_numpy(dtype=float)
    order = np.argsort(x)
    x = x[order]
    u = u[order]
    v = v[order]
    pressure = pressure[order]

    good = np.isfinite(x) & np.isfinite(u) & np.isfinite(v)
    x = x[good]
    u = u[good]
    v = v[good]
    pressure = pressure[good]
    heights = np.asarray(obs_heights, dtype=float)
    if len(x) < 1:
        return pd.DataFrame({"height_m": heights, "gfs_speed": np.nan, "gfs_dir": np.nan, "pressure_hpa": np.nan})

    interp_u = []
    interp_v = []
    methods = []
    lower_heights = []
    upper_heights = []
    for height in heights:
        uu, method, lower_h, upper_h = interpolate_or_nearest(height, x, u)
        vv, _, _, _ = interpolate_or_nearest(height, x, v)
        interp_u.append(uu)
        interp_v.append(vv)
        methods.append(method)
        lower_heights.append(lower_h)
        upper_heights.append(upper_h)
    interp_u = np.asarray(interp_u, dtype=float)
    interp_v = np.asarray(interp_v, dtype=float)

    pressure_good = np.isfinite(x) & np.isfinite(pressure)
    if pressure_good.sum() >= 2:
        interp_pressure = np.interp(heights, x[pressure_good], pressure[pressure_good], left=np.nan, right=np.nan)
    else:
        interp_pressure = np.full_like(heights, np.nan, dtype=float)

    return pd.DataFrame(
        {
            "height_m": heights,
            "gfs_speed": np.hypot(interp_u, interp_v),
            "gfs_dir": [
                wind_direction_from_uv(uu, vv) if np.isfinite(uu) and np.isfinite(vv) else np.nan
                for uu, vv in zip(interp_u, interp_v)
            ],
            "gfs_u": interp_u,
            "gfs_v": interp_v,
            "pressure_hpa": interp_pressure,
            "vertical_match_method": methods,
            "gfs_lower_height_m": lower_heights,
            "gfs_upper_height_m": upper_heights,
        }
    )


def read_gfs_predictions(gfs_dir, heights, lat, lon, valid_start_utc, valid_end_utc, spatial_aggregation, args):
    rows = []
    profile_rows = []
    for path in sorted(Path(gfs_dir).rglob("*.grib2")):
        inferred = infer_valid_time_from_path(path)
        if inferred is not None and (inferred < valid_start_utc or inferred >= valid_end_utc):
            continue

        print(f"[GFS] {path.name}")
        valid_time, profile = read_gfs_profile(path, lat, lon, spatial_aggregation, args)
        if valid_time < pd.Timestamp(valid_start_utc) or valid_time >= pd.Timestamp(valid_end_utc):
            continue

        forecast_hour = int(re.search(r"\.f(\d{3})\.grib2$", path.name).group(1))
        cycle_utc = re.search(r"gfs\.0p25\.(\d{10})\.f", path.name).group(1)
        profile_out = profile.copy()
        profile_out["valid_time_utc"] = valid_time
        profile_out["forecast_hour"] = forecast_hour
        profile_out["cycle_utc"] = cycle_utc
        profile_out["grib_file"] = path.name
        profile_rows.append(profile_out)

        pred = interpolate_profile(profile, heights)
        pred["valid_time_utc"] = valid_time
        pred["gfs_spatial_aggregation"] = spatial_aggregation
        pred["forecast_hour"] = forecast_hour
        pred["cycle_utc"] = cycle_utc
        rows.append(pred)

    if not rows:
        return (
            pd.DataFrame(columns=["valid_time_utc", "height_m", "gfs_speed", "gfs_dir"]),
            pd.DataFrame(),
        )
    profiles = pd.concat(profile_rows, ignore_index=True) if profile_rows else pd.DataFrame()
    return pd.concat(rows, ignore_index=True), profiles


def expected_3hour_times(valid_start_utc, valid_end_utc):
    start = pd.Timestamp(valid_start_utc)
    end = pd.Timestamp(valid_end_utc)
    cur = start
    if cur.hour % 3 != 0:
        cur = cur.ceil("3h")
    values = []
    while cur < end:
        values.append(cur)
        cur += pd.Timedelta(hours=3)
    return values


def expected_source_candidates(valid_time, forecast_hours=(3, 6), cycle_hours=GFS_CYCLE_HOURS):
    ts = pd.Timestamp(valid_time)
    candidates = []
    for forecast_hour in forecast_hours:
        cycle = ts - pd.Timedelta(hours=forecast_hour)
        if cycle.hour not in cycle_hours:
            continue
        candidates.append(
            f"{cycle:%Y%m%d}/gfs.0p25.{cycle:%Y%m%d%H}.f{forecast_hour:03d}.grib2"
        )
    return candidates


def compute_metrics(pairs):
    err = pairs["gfs_speed"] - pairs["obs_speed"]
    dir_abs = pairs["dir_diff"].abs().dropna()
    return pd.Series(
        {
            "count": int(len(pairs)),
            "mse": float(np.mean(np.square(err))),
            "rmse": float(np.sqrt(np.mean(np.square(err)))),
            "mae": float(np.mean(np.abs(err))),
            "bias": float(np.mean(err)),
            "corr": float(pairs["gfs_speed"].corr(pairs["obs_speed"])) if len(pairs) > 1 else np.nan,
            "dir_count": int(len(dir_abs)),
            "dir_mae_deg": float(dir_abs.mean()) if len(dir_abs) else np.nan,
            "dir_bias_deg": float(pairs["dir_diff"].dropna().mean()) if len(dir_abs) else np.nan,
        }
    )


def compute_unit_sensitivity(pairs):
    tests = [
        ("m/s_as_is", 1.0),
        ("knots_to_m/s", 0.514444),
        ("km/h_to_m/s", 1.0 / 3.6),
        ("mph_to_m/s", 0.44704),
    ]
    raw_obs = pairs["obs_speed_raw"] if "obs_speed_raw" in pairs.columns else pairs["obs_speed"]
    rows = []
    for name, scale in tests:
        test_pairs = pairs.copy()
        test_pairs["obs_speed"] = raw_obs * scale
        metrics = compute_metrics(test_pairs).to_dict()
        metrics.update(
            {
                "obs_unit_assumption": name,
                "obs_speed_scale_to_mps": scale,
                "mean_obs_speed_mps": float(test_pairs["obs_speed"].mean()),
                "mean_gfs_speed_mps": float(test_pairs["gfs_speed"].mean()),
                "mean_gfs_obs_ratio": float((test_pairs["gfs_speed"] / test_pairs["obs_speed"]).replace([np.inf, -np.inf], np.nan).mean()),
            }
        )
        rows.append(metrics)
    return pd.DataFrame(rows)


def plot_speed_by_height(pairs, out_dir, max_plots=None):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError as exc:
        raise SystemExit("绘图需要 matplotlib：conda install -c conda-forge matplotlib") from exc

    plot_dir = Path(out_dir) / "plots_by_height"
    plot_dir.mkdir(parents=True, exist_ok=True)

    grouped = sorted(pairs.groupby("height_m"), key=lambda item: item[0])
    if max_plots is not None:
        grouped = grouped[:max_plots]

    for height, data in grouped:
        data = data.sort_values("valid_time_utc").copy()
        plot_time = pd.to_datetime(data["valid_time_utc"]).dt.tz_convert(BEIJING_TZ).dt.tz_localize(None)

        fig, ax = plt.subplots(figsize=(10, 4.8), dpi=150)
        ax.plot(plot_time, data["gfs_speed"], marker="o", linewidth=1.8, label="GFS")
        ax.plot(plot_time, data["obs_speed"], marker="s", linewidth=1.8, label="Observed")
        ax.set_title(f"Wind Speed at {height:g} m")
        ax.set_xlabel("Beijing Time")
        ax.set_ylabel("Wind Speed (m/s)")
        ax.grid(True, alpha=0.3)
        ax.legend()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        fig.autofmt_xdate()
        fig.tight_layout()

        filename = f"wind_speed_{height:g}m.png".replace(".", "p")
        fig.savefig(plot_dir / filename)
        plt.close(fig)

    print(f"[PLOTS] {plot_dir} ({len(grouped)} files)")


def date_range_texts(start_date, end_date):
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    values = []
    cur = start
    while cur <= end:
        values.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return values


def beijing_range_window(start_date, end_date):
    start_bj = datetime.strptime(start_date, "%Y%m%d").replace(tzinfo=BEIJING_TZ)
    end_bj = datetime.strptime(end_date, "%Y%m%d").replace(tzinfo=BEIJING_TZ) + timedelta(days=1)
    return start_bj, end_bj, start_bj.astimezone(timezone.utc), end_bj.astimezone(timezone.utc)


def add_error_columns(pairs, obs_speed_scale):
    pairs = pairs.copy()
    pairs["obs_speed_raw"] = pairs["obs_speed"]
    if not np.isclose(obs_speed_scale, 1.0):
        pairs["obs_speed"] = pairs["obs_speed"] * obs_speed_scale
    pairs["speed_diff"] = pairs["gfs_speed"] - pairs["obs_speed"]
    pairs["dir_diff"] = circular_direction_diff(pairs["gfs_dir"], pairs["obs_dir"])
    pairs["abs_dir_diff"] = pairs["dir_diff"].abs()
    if "sample_count" not in pairs.columns:
        pairs["sample_count"] = 1
    pairs["compare_date"] = pd.to_datetime(pairs["valid_time_utc"]).dt.tz_convert(BEIJING_TZ).dt.strftime("%Y%m%d")
    return pairs


def write_full_range_outputs(
    pairs,
    pred,
    gfs_profiles,
    missing_times,
    start_date,
    end_date,
    output_dir,
    args,
):
    out_dir = Path(output_dir) / f"{start_date}_{end_date}"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = compute_metrics(pairs).to_frame().T
    by_height = pairs.groupby("height_m").apply(compute_metrics, include_groups=False).reset_index()
    by_date = pairs.groupby("compare_date").apply(compute_metrics, include_groups=False).reset_index()
    by_time = pairs.groupby("valid_time_utc").apply(compute_metrics, include_groups=False).reset_index()
    unit_sensitivity = compute_unit_sensitivity(pairs)

    pairs.to_csv(out_dir / "gfs_obs_speed_direction_pairs_all.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out_dir / "metrics_summary_all.csv", index=False, encoding="utf-8-sig")
    by_height.to_csv(out_dir / "metrics_by_height_all.csv", index=False, encoding="utf-8-sig")
    by_date.to_csv(out_dir / "metrics_by_date.csv", index=False, encoding="utf-8-sig")
    by_time.to_csv(out_dir / "metrics_by_time.csv", index=False, encoding="utf-8-sig")
    unit_sensitivity.to_csv(out_dir / "unit_sensitivity_all.csv", index=False, encoding="utf-8-sig")

    pred_height_summary = (
        pred.groupby("height_m", as_index=False)
        .agg(
            gfs_count=("gfs_speed", "count"),
            gfs_valid_count=("gfs_speed", lambda values: int(values.notna().sum())),
            min_gfs_speed=("gfs_speed", "min"),
            max_gfs_speed=("gfs_speed", "max"),
            vertical_match_methods=("vertical_match_method", lambda values: ";".join(sorted(set(values.dropna().astype(str))))),
            gfs_lower_height_m=("gfs_lower_height_m", "min"),
            gfs_upper_height_m=("gfs_upper_height_m", "max"),
        )
    )
    pred_height_summary.to_csv(out_dir / "gfs_prediction_height_summary_all.csv", index=False, encoding="utf-8-sig")

    matched_height_summary = (
        pairs.groupby("height_m", as_index=False)
        .agg(
            matched_count=("gfs_speed", "count"),
            obs_sample_count_total=("sample_count", "sum"),
            min_obs_speed=("obs_speed", "min"),
            max_obs_speed=("obs_speed", "max"),
            min_gfs_speed=("gfs_speed", "min"),
            max_gfs_speed=("gfs_speed", "max"),
            vertical_match_methods=("vertical_match_method", lambda values: ";".join(sorted(set(values.dropna().astype(str))))),
            gfs_lower_height_m=("gfs_lower_height_m", "min"),
            gfs_upper_height_m=("gfs_upper_height_m", "max"),
        )
    )
    matched_height_summary.to_csv(out_dir / "matched_height_summary_all.csv", index=False, encoding="utf-8-sig")

    if not gfs_profiles.empty and args.save_gfs_profiles:
        gfs_profiles.to_csv(out_dir / "gfs_profile_levels_all.csv", index=False, encoding="utf-8-sig")

    missing_rows = []
    for ts in missing_times:
        missing_rows.append(
            {
                "valid_time_utc": ts.strftime("%Y-%m-%d %H:%M"),
                "valid_time_bj": ts.tz_convert(BEIJING_TZ).strftime("%Y-%m-%d %H:%M"),
                "candidate_files": " 或 ".join(expected_source_candidates(ts)),
            }
        )
    pd.DataFrame(missing_rows).to_csv(out_dir / "missing_gfs_valid_times.csv", index=False, encoding="utf-8-sig")

    if args.plots:
        plot_speed_by_height(pairs, out_dir, max_plots=args.max_plots)

    print("=" * 100)
    print(f"[OUTPUT] {out_dir}")
    print("[METRICS] all-range summary")
    print(summary.to_string(index=False))
    print(f"Matched rows: {len(pairs)}")
    print(f"Matched GFS valid times: {pairs['valid_time_utc'].nunique()}")
    print(f"Missing expected GFS valid times: {len(missing_times)}")
    return out_dir, summary


def run_compare_for_range_once(args, wgrib2_path=None):
    start_bj, end_bj, valid_start_utc, valid_end_utc = beijing_range_window(args.start_date, args.end_date)
    print("=" * 100)
    print(f"Full-range validation: {args.start_date} -> {args.end_date}")
    print(f"Beijing window: {start_bj:%Y-%m-%d %H:%M} -> {end_bj:%Y-%m-%d %H:%M}")
    print(f"UTC window: {valid_start_utc:%Y-%m-%d %H:%M} -> {valid_end_utc:%Y-%m-%d %H:%M}")
    print(f"GFS dir: {args.gfs_dir}")
    print(f"OBS dir: {args.obs_dir}")
    print(f"OBS format: {args.obs_format} (auto: Jul-Oct=new, Nov-Feb=old)")
    print(f"GFS spatial aggregation: {args.gfs_spatial_aggregation}")
    print(f"OBS aggregation: {args.obs_aggregation}")
    print(f"OBS speed scale to m/s: {args.obs_speed_scale}")
    print(f"Low-level reader: {args.low_level_reader}")
    if wgrib2_path:
        print(f"wgrib2: {wgrib2_path}")

    obs = load_observations_for_range(args.obs_dir, args.start_date, args.end_date, args.obs_format)
    if obs.empty:
        raise RuntimeError(f"没有读取到 {args.start_date} 至 {args.end_date} 的实测风速。")
    obs_start_utc = valid_start_utc
    if args.obs_aggregation == "previous_3h_mean":
        obs_start_utc = valid_start_utc - timedelta(hours=args.obs_window_hours)
    obs = obs[(obs["time_utc"] >= obs_start_utc) & (obs["time_utc"] <= valid_end_utc)].copy()
    if obs.empty:
        raise RuntimeError("日期范围对应 UTC 窗口内没有实测风速。")
    heights = sorted(obs["height_m"].dropna().unique())
    print(f"[OBS] rows={len(obs)}, heights={len(heights)}, height_range={min(heights):g}-{max(heights):g} m")

    expected_times = expected_3hour_times(valid_start_utc, valid_end_utc)
    pred, gfs_profiles = read_gfs_predictions(
        args.gfs_dir,
        heights,
        args.lat,
        args.lon,
        valid_start_utc,
        valid_end_utc,
        args.gfs_spatial_aggregation,
        args,
    )
    if pred.empty:
        raise RuntimeError("没有读取到该日期范围内的 GFS GRIB2。")

    target_times = sorted(pred["valid_time_utc"].dropna().unique())
    missing_times = [ts for ts in expected_times if ts not in {pd.Timestamp(v) for v in target_times}]
    print(f"[GFS] valid_times={len(target_times)}, expected={len(expected_times)}, missing={len(missing_times)}")
    if missing_times:
        print("[WARN] Missing expected 3-hour GFS valid times:")
        for ts in missing_times[: args.missing_print_limit]:
            print(f"  UTC {ts:%Y-%m-%d %H:%M} / Beijing {ts.tz_convert(BEIJING_TZ):%Y-%m-%d %H:%M}")
            candidates = expected_source_candidates(ts)
            if candidates:
                print(f"    需要存在并完成裁剪: {' 或 '.join(candidates)}")
        if len(missing_times) > args.missing_print_limit:
            print(f"  ... 还有 {len(missing_times) - args.missing_print_limit} 个缺失时刻，详见输出 CSV")

    obs_match = aggregate_observations(obs, args.obs_aggregation, target_times, args.obs_window_hours)
    if obs_match.empty:
        raise RuntimeError("GFS 的 3 小时间隔有效时刻没有匹配到实测观测。")

    pairs = pred.merge(obs_match, on=["valid_time_utc", "height_m"], how="inner")
    pairs = pairs.dropna(subset=["gfs_speed", "obs_speed"])
    if pairs.empty:
        raise RuntimeError("GFS 与实测没有匹配上的时间和高度。")
    pairs = add_error_columns(pairs, args.obs_speed_scale)

    return write_full_range_outputs(
        pairs,
        pred,
        gfs_profiles,
        missing_times,
        args.start_date,
        args.end_date,
        args.output_dir,
        args,
    )


def print_run_header(args, compare_date, wgrib2_path=None):
    start_bj, end_bj, valid_start_utc, valid_end_utc = beijing_day_window(compare_date)
    print("=" * 100)
    print(f"Beijing date: {compare_date} ({start_bj:%Y-%m-%d %H:%M} -> {end_bj:%Y-%m-%d %H:%M})")
    print(f"UTC window: {valid_start_utc:%Y-%m-%d %H:%M} -> {valid_end_utc:%Y-%m-%d %H:%M}")
    print(f"GFS dir: {args.gfs_dir}")
    print(f"OBS dir: {args.obs_dir}")
    print(f"OBS format: {args.obs_format} (auto: Jul-Oct=new, Nov-Feb=old)")
    print(f"GFS spatial aggregation: {args.gfs_spatial_aggregation}")
    print(f"OBS aggregation: {args.obs_aggregation}")
    print(f"OBS speed scale to m/s: {args.obs_speed_scale}")
    print(f"Low-level reader: {args.low_level_reader}")
    if wgrib2_path:
        print(f"wgrib2: {wgrib2_path}")
    return start_bj, end_bj, valid_start_utc, valid_end_utc


def run_compare_for_date(args, compare_date, wgrib2_path=None):
    _, _, valid_start_utc, valid_end_utc = print_run_header(args, compare_date, wgrib2_path)

    obs = load_observations(args.obs_dir, compare_date, args.obs_format)
    if obs.empty:
        raise RuntimeError(f"没有读取到北京时间 {compare_date} 的实测风速。")
    obs_start_utc = valid_start_utc
    if args.obs_aggregation == "previous_3h_mean":
        obs_start_utc = valid_start_utc - timedelta(hours=args.obs_window_hours)
    obs = obs[(obs["time_utc"] >= obs_start_utc) & (obs["time_utc"] <= valid_end_utc)].copy()
    if obs.empty:
        raise RuntimeError(f"北京时间 {compare_date} 对应 UTC 窗口内没有实测风速。")
    heights = sorted(obs["height_m"].dropna().unique())

    pred, gfs_profiles = read_gfs_predictions(
        args.gfs_dir,
        heights,
        args.lat,
        args.lon,
        valid_start_utc,
        valid_end_utc,
        args.gfs_spatial_aggregation,
        args,
    )
    if pred.empty:
        raise RuntimeError("没有读取到该日期窗口内的 GFS GRIB2。")
    target_times = sorted(pred["valid_time_utc"].dropna().unique())
    print(f"[MATCH] GFS valid times in Beijing day: {len(target_times)}")
    for valid_time in target_times:
        ts = pd.Timestamp(valid_time)
        print(f"  UTC {ts:%Y-%m-%d %H:%M} / Beijing {ts.tz_convert(BEIJING_TZ):%Y-%m-%d %H:%M}")
    expected_times = expected_3hour_times(valid_start_utc, valid_end_utc)
    missing_times = [ts for ts in expected_times if ts not in {pd.Timestamp(v) for v in target_times}]
    if missing_times:
        print("[WARN] Missing expected 3-hour GFS valid times:")
        for ts in missing_times:
            print(f"  UTC {ts:%Y-%m-%d %H:%M} / Beijing {ts.tz_convert(BEIJING_TZ):%Y-%m-%d %H:%M}")
            candidates = expected_source_candidates(ts)
            if candidates:
                print(f"    需要存在并完成裁剪: {' 或 '.join(candidates)}")

    obs_match = aggregate_observations(obs, args.obs_aggregation, target_times, args.obs_window_hours)
    if obs_match.empty:
        raise RuntimeError("GFS 的 3 小时间隔有效时刻没有匹配到实测观测。")

    pairs = pred.merge(obs_match, on=["valid_time_utc", "height_m"], how="inner")
    pairs = pairs.dropna(subset=["gfs_speed", "obs_speed"])
    if pairs.empty:
        raise RuntimeError("GFS 与实测没有匹配上的时间和高度。")

    pairs["obs_speed_raw"] = pairs["obs_speed"]
    if not np.isclose(args.obs_speed_scale, 1.0):
        pairs["obs_speed"] = pairs["obs_speed"] * args.obs_speed_scale

    pairs["speed_diff"] = pairs["gfs_speed"] - pairs["obs_speed"]
    pairs["dir_diff"] = circular_direction_diff(pairs["gfs_dir"], pairs["obs_dir"])
    pairs["abs_dir_diff"] = pairs["dir_diff"].abs()
    if "sample_count" not in pairs.columns:
        pairs["sample_count"] = 1
    pairs["compare_date"] = compare_date

    summary = compute_metrics(pairs).to_frame().T
    summary.insert(0, "compare_date", compare_date)
    by_height = pairs.groupby("height_m").apply(compute_metrics, include_groups=False).reset_index()
    by_time = pairs.groupby("valid_time_utc").apply(compute_metrics, include_groups=False).reset_index()
    unit_sensitivity = compute_unit_sensitivity(pairs)

    out_dir = Path(args.output_dir) / compare_date
    out_dir.mkdir(parents=True, exist_ok=True)
    if not gfs_profiles.empty:
        gfs_profiles.to_csv(out_dir / "gfs_profile_levels.csv", index=False, encoding="utf-8-sig")
    pred_height_summary = (
        pred.groupby("height_m", as_index=False)
        .agg(
            gfs_count=("gfs_speed", "count"),
            gfs_valid_count=("gfs_speed", lambda values: int(values.notna().sum())),
            min_gfs_speed=("gfs_speed", "min"),
            max_gfs_speed=("gfs_speed", "max"),
            vertical_match_methods=("vertical_match_method", lambda values: ";".join(sorted(set(values.dropna().astype(str))))),
            gfs_lower_height_m=("gfs_lower_height_m", "min"),
            gfs_upper_height_m=("gfs_upper_height_m", "max"),
        )
    )
    pred_height_summary.to_csv(out_dir / "gfs_prediction_height_summary.csv", index=False, encoding="utf-8-sig")
    pairs.to_csv(out_dir / "gfs_obs_speed_direction_pairs.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out_dir / "metrics_summary.csv", index=False, encoding="utf-8-sig")
    by_height.to_csv(out_dir / "metrics_by_height.csv", index=False, encoding="utf-8-sig")
    by_time.to_csv(out_dir / "metrics_by_time.csv", index=False, encoding="utf-8-sig")
    unit_sensitivity.to_csv(out_dir / "unit_sensitivity.csv", index=False, encoding="utf-8-sig")
    matched_height_summary = (
        pairs.groupby("height_m", as_index=False)
        .agg(
            matched_count=("gfs_speed", "count"),
            obs_sample_count_total=("sample_count", "sum"),
            min_obs_speed=("obs_speed", "min"),
            max_obs_speed=("obs_speed", "max"),
            min_gfs_speed=("gfs_speed", "min"),
            max_gfs_speed=("gfs_speed", "max"),
            vertical_match_methods=("vertical_match_method", lambda values: ";".join(sorted(set(values.dropna().astype(str))))),
            gfs_lower_height_m=("gfs_lower_height_m", "min"),
            gfs_upper_height_m=("gfs_upper_height_m", "max"),
        )
    )
    matched_height_summary.to_csv(out_dir / "matched_height_summary.csv", index=False, encoding="utf-8-sig")
    if not args.no_plots:
        plot_speed_by_height(pairs, out_dir, max_plots=args.max_plots)

    print("[METRICS] summary")
    print(summary.to_string(index=False))
    cols = [
        "valid_time_utc",
        "time_bj",
        "obs_window_start_utc",
        "obs_window_end_utc",
        "height_m",
        "vertical_match_method",
        "gfs_lower_height_m",
        "gfs_upper_height_m",
        "forecast_hour",
        "sample_count",
        "obs_speed_raw",
        "obs_speed",
        "gfs_speed",
        "speed_diff",
        "obs_dir",
        "gfs_dir",
        "dir_diff",
        "abs_dir_diff",
        "pressure_hpa",
    ]
    print("[SAMPLE]")
    cols = [col for col in cols if col in pairs.columns]
    print(pairs[cols].sort_values(["valid_time_utc", "height_m"]).head(args.sample_rows).to_string(index=False))
    print(f"[OUTPUT] {out_dir}")
    return pairs, summary, missing_times


def write_range_outputs(all_pairs, daily_summaries, failures, start_date, end_date, output_dir):
    range_dir = Path(output_dir) / f"{start_date}_{end_date}"
    range_dir.mkdir(parents=True, exist_ok=True)
    pairs = pd.concat(all_pairs, ignore_index=True) if all_pairs else pd.DataFrame()
    daily = pd.concat(daily_summaries, ignore_index=True) if daily_summaries else pd.DataFrame()
    if not pairs.empty:
        pairs.to_csv(range_dir / "gfs_obs_speed_direction_pairs_all.csv", index=False, encoding="utf-8-sig")
        compute_metrics(pairs).to_frame().T.to_csv(range_dir / "metrics_summary_all.csv", index=False, encoding="utf-8-sig")
        pairs.groupby("height_m").apply(compute_metrics, include_groups=False).reset_index().to_csv(
            range_dir / "metrics_by_height_all.csv", index=False, encoding="utf-8-sig"
        )
        pairs.groupby("compare_date").apply(compute_metrics, include_groups=False).reset_index().to_csv(
            range_dir / "metrics_by_date.csv", index=False, encoding="utf-8-sig"
        )
        compute_unit_sensitivity(pairs).to_csv(range_dir / "unit_sensitivity_all.csv", index=False, encoding="utf-8-sig")
    if not daily.empty:
        daily.to_csv(range_dir / "daily_metrics_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(failures, columns=["compare_date", "reason"]).to_csv(
        range_dir / "failed_dates.csv", index=False, encoding="utf-8-sig"
    )
    print("=" * 100)
    print(f"[RANGE OUTPUT] {range_dir}")
    print(f"Successful days: {len(daily_summaries)}")
    print(f"Failed days: {len(failures)}")
    if not pairs.empty:
        print("[RANGE METRICS] summary")
        print(compute_metrics(pairs).to_frame().T.to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description="验证 GFS 与测风激光雷达的风速、风向差距。")
    parser.add_argument("--gfs-dir", default="./data/gdex_gfs_0p25_windcheck")
    parser.add_argument(
        "--obs-dir",
        default="./",
        help="实测风速根目录；默认当前目录。auto 格式下 7-10 月读 ./YYYY-MM，新格式；11-2 月读 ./202511~202602，旧格式",
    )
    parser.add_argument(
        "--obs-format",
        choices=["auto", "new", "old"],
        default="auto",
        help="实测文件格式；auto 表示 7-10 月使用新长表，11-2 月使用旧宽表",
    )
    parser.add_argument("--output-dir", default="./data/gfs_wind_validation")
    parser.add_argument("--start-date", default="20251101", help="范围验证起始北京时间日期 YYYYMMDD")
    parser.add_argument("--end-date", default="20260228", help="范围验证结束北京时间日期 YYYYMMDD")
    parser.add_argument("--lat", type=float, default=31.0)
    parser.add_argument("--lon", type=float, default=118.5)
    parser.add_argument("--wgrib2", default=None, help="wgrib2 路径；低空固定高度层默认优先用 wgrib2 读取")
    parser.add_argument("--library-dir", default=None, help="额外加入 LD_LIBRARY_PATH 的 lib 目录")
    parser.add_argument(
        "--low-level-reader",
        choices=["auto", "wgrib2", "cfgrib"],
        default="auto",
        help="低空固定高度层读取方式；默认 auto，优先 wgrib2，失败后 cfgrib",
    )
    parser.add_argument(
        "--obs-aggregation",
        choices=["previous_3h_mean", "nearest_hour", "hourly_mean"],
        default="previous_3h_mean",
        help="实测聚合方式；默认对每个 GFS 有效时刻取前 3 小时平均",
    )
    parser.add_argument("--obs-window-hours", type=float, default=3.0, help="previous_3h_mean 的时间窗口小时数")
    parser.add_argument(
        "--gfs-spatial-aggregation",
        choices=["region_mean", "nearest"],
        default="region_mean",
        help="GFS 裁剪区域内的空间聚合；默认对区域内格点平均",
    )
    parser.add_argument("--sample-rows", type=int, default=50)
    parser.add_argument("--plots", action="store_true", help="生成整个时段每个高度的风速时间序列图；默认不绘图")
    parser.add_argument("--max-plots", type=int, default=None, help="最多绘制多少个高度；默认绘制所有高度")
    parser.add_argument("--missing-print-limit", type=int, default=80, help="终端最多打印多少个缺失 GFS 时刻")
    parser.add_argument("--save-gfs-profiles", action="store_true", help="保存每个 GRIB 的 GFS 垂直廓线明细；默认不保存以减少输出体积")
    parser.add_argument(
        "--obs-speed-scale",
        type=float,
        default=1.0,
        help="实测风速乘到 m/s 的比例；默认 1.0。若确认实测为 knots，可设为 0.514444",
    )
    parser.add_argument(
        "--include-pressure-ground-layer",
        action="store_true",
        help="尝试读取 30-0 mb above ground 层；该层不是固定高度，默认跳过以避免高度不可判定的警告",
    )
    args = parser.parse_args()

    wgrib2_path = find_wgrib2(args.wgrib2)
    if args.low_level_reader == "wgrib2" and not wgrib2_path:
        raise SystemExit("指定了 --low-level-reader wgrib2，但未找到 wgrib2。请用 --wgrib2 指定路径。")

    try:
        run_compare_for_range_once(args, wgrib2_path=wgrib2_path)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
