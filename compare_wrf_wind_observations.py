#!/usr/bin/env python3
import argparse
import math
from datetime import timezone
from pathlib import Path

import numpy as np
import pandas as pd

from compare_gfs_wind_one_day import (
    BEIJING_TZ,
    aggregate_observations,
    circular_direction_diff,
    load_observations_for_range,
)


GRAVITY = 9.81
DEFAULT_CORE_MIN_HEIGHT_M = 30
DEFAULT_CORE_MAX_HEIGHT_M = 120
DEFAULT_CORE_HEIGHT_STEP_M = 10
OBS_DIRECTION_CONVENTIONS = (
    "meteorological_from",
    "meteorological_to",
    "mathematical_from",
    "mathematical_to",
)


def build_core_heights(min_height, max_height, step):
    if min_height <= 0 or max_height < min_height or step <= 0:
        raise ValueError("核心层高度参数必须满足：0 < min <= max，step > 0")
    heights = np.arange(min_height, max_height + step * 0.5, step, dtype=float)
    return heights[heights <= max_height + 1e-6].tolist()


def select_core_observations(observations, core_heights, tolerance_m=0.1, require_complete=False):
    selected = []
    missing = []
    available = observations["height_m"].dropna().astype(float).to_numpy()
    for height in core_heights:
        matches = observations[np.isclose(observations["height_m"].astype(float), height, atol=tolerance_m)]
        if matches.empty:
            missing.append(height)
            continue
        matches = matches.copy()
        matches["height_m"] = float(height)
        selected.append(matches)

    if missing and require_complete:
        available_core = sorted(
            set(float(value) for value in available if core_heights[0] <= value <= core_heights[-1])
        )
        raise SystemExit(
            "实测数据未完整覆盖低空核心层。"
            f"缺失高度: {', '.join(f'{value:g}m' for value in missing)}；"
            f"当前范围内可用高度: {available_core}。"
            "请使用 --obs-format new 读取 KC-WL-3D 新版实测文件。"
        )
    if not selected:
        return observations.iloc[0:0].copy()
    return pd.concat(selected, ignore_index=True)


def open_wrf_dataset(path):
    try:
        from netCDF4 import Dataset
    except ImportError as exc:
        raise SystemExit(
            "读取 wrfout 需要 Python netCDF4："
            "conda install -c conda-forge netcdf4 numpy pandas openpyxl matplotlib"
        ) from exc
    return Dataset(path, "r")


def as_array(values):
    return np.asarray(np.ma.filled(values, np.nan), dtype=float)


def decode_time_row(row):
    values = np.asarray(row)
    if values.dtype.kind == "S":
        return b"".join(values.tolist()).decode("ascii", errors="replace").strip()
    return "".join(values.astype(str).tolist()).strip()


def read_wrf_times(dataset):
    if "Times" not in dataset.variables:
        raise RuntimeError(f"{dataset.filepath()} does not contain Times")
    raw = np.asarray(dataset.variables["Times"][:])
    if raw.ndim == 1:
        raw = raw[np.newaxis, :]
    return [
        pd.Timestamp(decode_time_row(row).replace("_", "T"), tz=timezone.utc)
        for row in raw
    ]


def read_time_slice(variable, time_index):
    if variable.ndim >= 3 and variable.dimensions[0] == "Time":
        return as_array(variable[time_index])
    return as_array(variable[:])


def haversine_km(lat1, lon1, lat2, lon2):
    radius_km = 6371.0
    lat1r = np.deg2rad(lat1)
    lon1r = np.deg2rad(lon1)
    lat2r = np.deg2rad(lat2)
    lon2r = np.deg2rad(lon2)
    dlat = lat2r - lat1r
    dlon = lon2r - lon1r
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2.0) ** 2
    a = np.clip(a, 0.0, 1.0)
    return radius_km * 2.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))


def nearest_grid_cell(dataset, lat, lon):
    xlat = read_time_slice(dataset.variables["XLAT"], 0)
    xlon = read_time_slice(dataset.variables["XLONG"], 0)
    distances = haversine_km(lat, lon, xlat, xlon)
    j, i = np.unravel_index(np.nanargmin(distances), distances.shape)
    return int(j), int(i), float(xlat[j, i]), float(xlon[j, i]), float(distances[j, i])


def neighborhood_cells(j, i, ny, nx, radius):
    cells = [
        (jj, ii)
        for jj in range(max(0, j - radius), min(ny, j + radius + 1))
        for ii in range(max(0, i - radius), min(nx, i + radius + 1))
    ]
    return sorted(cells, key=lambda cell: (cell[0] - j) ** 2 + (cell[1] - i) ** 2)


def validate_wrf_staggering(dataset):
    ny, nx = read_time_slice(dataset.variables["XLAT"], 0).shape
    u_shape = dataset.variables["U"].shape
    v_shape = dataset.variables["V"].shape
    u_valid = u_shape[-2:] == (ny, nx + 1)
    v_valid = v_shape[-2:] == (ny + 1, nx)
    if not u_valid or not v_valid:
        raise RuntimeError(
            "WRF U/V维度不符合Arakawa C网格预期："
            f"mass=({ny},{nx}), U={u_shape[-2:]}, V={v_shape[-2:]}"
        )
    return {
        "mass_grid_shape": f"{ny}x{nx}",
        "u_horizontal_shape": f"{u_shape[-2]}x{u_shape[-1]}",
        "v_horizontal_shape": f"{v_shape[-2]}x{v_shape[-1]}",
        "u_destagger_axis": "west_east_stag",
        "v_destagger_axis": "south_north_stag",
        "stagger_shape_check_passed": True,
    }


def rotate_to_earth(u, v, cosalpha, sinalpha):
    return u * cosalpha - v * sinalpha, v * cosalpha + u * sinalpha


def convert_direction_to_meteorological_from(direction, convention):
    direction = np.asarray(direction, dtype=float)
    if convention == "meteorological_from":
        return direction % 360.0
    if convention == "meteorological_to":
        return (direction + 180.0) % 360.0
    if convention == "mathematical_from":
        return (90.0 - direction) % 360.0
    if convention == "mathematical_to":
        return (270.0 - direction) % 360.0
    raise ValueError(f"未知观测风向定义: {convention}")


def meteorological_from_to_uv(speed, direction):
    radians = np.deg2rad(direction)
    return -speed * np.sin(radians), -speed * np.cos(radians)


def mean_duplicate_heights(heights, u, v):
    frame = pd.DataFrame({"height": heights, "u": u, "v": v})
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["height", "u", "v"])
    frame = frame.groupby("height", as_index=False).agg(u=("u", "mean"), v=("v", "mean"))
    return frame.sort_values("height")


def interpolate_profile(heights, u, v, target_heights):
    profile = mean_duplicate_heights(heights, u, v)
    targets = np.asarray(target_heights, dtype=float)
    out_u = np.full(targets.shape, np.nan)
    out_v = np.full(targets.shape, np.nan)
    lower = np.full(targets.shape, np.nan)
    upper = np.full(targets.shape, np.nan)
    methods = np.full(targets.shape, "outside_profile", dtype=object)

    if len(profile) == 0:
        return out_u, out_v, lower, upper, methods

    z = profile["height"].to_numpy(dtype=float)
    pu = profile["u"].to_numpy(dtype=float)
    pv = profile["v"].to_numpy(dtype=float)
    for index, target in enumerate(targets):
        if target < z[0] or target > z[-1]:
            continue
        exact = np.where(np.isclose(z, target, atol=0.01))[0]
        if len(exact):
            pos = int(exact[0])
            out_u[index], out_v[index] = pu[pos], pv[pos]
            lower[index] = upper[index] = z[pos]
            methods[index] = "exact"
            continue
        upper_index = int(np.searchsorted(z, target, side="right"))
        lower_index = upper_index - 1
        weight = (target - z[lower_index]) / (z[upper_index] - z[lower_index])
        out_u[index] = pu[lower_index] + weight * (pu[upper_index] - pu[lower_index])
        out_v[index] = pv[lower_index] + weight * (pv[upper_index] - pv[lower_index])
        lower[index], upper[index] = z[lower_index], z[upper_index]
        methods[index] = "linear"
    return out_u, out_v, lower, upper, methods


def cell_profile(dataset, time_index, j, i):
    u_stag = as_array(dataset.variables["U"][time_index, :, j, i : i + 2])
    v_stag = as_array(dataset.variables["V"][time_index, :, j : j + 2, i])
    grid_u = np.nanmean(u_stag, axis=-1)
    grid_v = np.nanmean(v_stag, axis=-1)

    ph = as_array(dataset.variables["PH"][time_index, :, j, i])
    phb = as_array(dataset.variables["PHB"][time_index, :, j, i])
    hgt = float(as_array(dataset.variables["HGT"][time_index, j, i]))
    z_interfaces_msl = (ph + phb) / GRAVITY
    heights_agl = 0.5 * (z_interfaces_msl[:-1] + z_interfaces_msl[1:]) - hgt

    cosalpha = 1.0
    sinalpha = 0.0
    if "COSALPHA" in dataset.variables and "SINALPHA" in dataset.variables:
        cosalpha = float(read_time_slice(dataset.variables["COSALPHA"], time_index)[j, i])
        sinalpha = float(read_time_slice(dataset.variables["SINALPHA"], time_index)[j, i])
    earth_u, earth_v = rotate_to_earth(grid_u, grid_v, cosalpha, sinalpha)

    if "U10" in dataset.variables and "V10" in dataset.variables:
        grid_u10 = float(read_time_slice(dataset.variables["U10"], time_index)[j, i])
        grid_v10 = float(read_time_slice(dataset.variables["V10"], time_index)[j, i])
        earth_u10, earth_v10 = rotate_to_earth(grid_u10, grid_v10, cosalpha, sinalpha)
        heights_agl = np.concatenate(([10.0], heights_agl))
        grid_u = np.concatenate(([grid_u10], grid_u))
        grid_v = np.concatenate(([grid_v10], grid_v))
        earth_u = np.concatenate(([earth_u10], earth_u))
        earth_v = np.concatenate(([earth_v10], earth_v))

    return heights_agl, grid_u, grid_v, earth_u, earth_v


def wind_direction(u, v):
    return (270.0 - np.degrees(np.arctan2(v, u))) % 360.0


def predict_at_heights(dataset, time_index, cells, target_heights):
    earth_results = []
    grid_results = []
    for j, i in cells:
        heights, grid_u, grid_v, earth_u, earth_v = cell_profile(dataset, time_index, j, i)
        grid_results.append(interpolate_profile(heights, grid_u, grid_v, target_heights))
        earth_results.append(interpolate_profile(heights, earth_u, earth_v, target_heights))

    all_u = np.stack([item[0] for item in earth_results])
    all_v = np.stack([item[1] for item in earth_results])
    all_grid_u = np.stack([item[0] for item in grid_results])
    all_grid_v = np.stack([item[1] for item in grid_results])
    with np.errstate(invalid="ignore"):
        model_u = np.nanmean(all_u, axis=0)
        model_v = np.nanmean(all_v, axis=0)
        model_grid_u = np.nanmean(all_grid_u, axis=0)
        model_grid_v = np.nanmean(all_grid_v, axis=0)

    lower = earth_results[0][2]
    upper = earth_results[0][3]
    methods = earth_results[0][4]
    return pd.DataFrame(
        {
            "height_m": np.asarray(target_heights, dtype=float),
            "wrf_u": model_u,
            "wrf_v": model_v,
            "wrf_speed": np.hypot(model_u, model_v),
            "wrf_dir": wind_direction(model_u, model_v),
            "wrf_grid_u": model_grid_u,
            "wrf_grid_v": model_grid_v,
            "wrf_grid_dir": wind_direction(model_grid_u, model_grid_v),
            "wrf_lower_height_m": lower,
            "wrf_upper_height_m": upper,
            "vertical_method": methods,
        }
    )


def find_wrfout_files(wrf_dir, domain):
    root = Path(wrf_dir)
    if root.is_file():
        return [root]
    files = sorted(root.rglob(f"wrfout_d{domain:02d}_*"))
    return [path for path in files if path.is_file()]


def read_wrf_predictions(args, target_heights):
    files = find_wrfout_files(args.wrf_dir, args.domain)
    if not files:
        raise SystemExit(f"没有找到 wrfout_d{args.domain:02d}_*：{args.wrf_dir}")

    predictions = []
    config_rows = []
    for path in files:
        print(f"[WRF] {path}")
        with open_wrf_dataset(path) as dataset:
            required = ["Times", "XLAT", "XLONG", "U", "V", "PH", "PHB", "HGT"]
            missing = [name for name in required if name not in dataset.variables]
            if missing:
                raise RuntimeError(f"{path} 缺少变量：{', '.join(missing)}")
            stagger_audit = validate_wrf_staggering(dataset)

            times = read_wrf_times(dataset)
            j, i, grid_lat, grid_lon, distance_km = nearest_grid_cell(dataset, args.lat, args.lon)
            if distance_km > args.max_grid_distance_km:
                raise RuntimeError(
                    f"最近WRF格点距离站点 {distance_km:.2f} km，超过限制 "
                    f"{args.max_grid_distance_km:.2f} km；请检查站点坐标和domain"
                )
            ny, nx = read_time_slice(dataset.variables["XLAT"], 0).shape
            cells = neighborhood_cells(j, i, ny, nx, args.neighborhood_radius)
            rotation_available = "COSALPHA" in dataset.variables and "SINALPHA" in dataset.variables
            cosalpha = (
                float(read_time_slice(dataset.variables["COSALPHA"], 0)[j, i])
                if rotation_available
                else 1.0
            )
            sinalpha = (
                float(read_time_slice(dataset.variables["SINALPHA"], 0)[j, i])
                if rotation_available
                else 0.0
            )
            config_row = {
                "source_file": str(path),
                "station_lat": args.lat,
                "station_lon": args.lon,
                "nearest_j": j,
                "nearest_i": i,
                "nearest_lat": grid_lat,
                "nearest_lon": grid_lon,
                "nearest_distance_km": distance_km,
                "neighborhood_cell_count": len(cells),
                "u_dimensions": "|".join(dataset.variables["U"].dimensions),
                "v_dimensions": "|".join(dataset.variables["V"].dimensions),
                "u_stagger_attribute": getattr(dataset.variables["U"], "stagger", ""),
                "v_stagger_attribute": getattr(dataset.variables["V"], "stagger", ""),
                "destagger_applied": True,
                "destagger_method": "U average west/east faces; V average south/north faces",
                "rotation_available": rotation_available,
                "cosalpha": cosalpha,
                "sinalpha": sinalpha,
                "rotation_angle_deg": math.degrees(math.atan2(sinalpha, cosalpha)),
                "rotation_formula": "earth_u=grid_u*cos-grid_v*sin; earth_v=grid_v*cos+grid_u*sin",
                "wrf_direction_definition": "meteorological_from_clockwise_from_true_north",
            }
            config_row.update(stagger_audit)
            config_rows.append(config_row)

            for time_index, valid_time in enumerate(times):
                profile = predict_at_heights(dataset, time_index, cells, target_heights)
                profile["valid_time_utc"] = valid_time
                profile["source_file"] = str(path)
                profile["nearest_grid_distance_km"] = distance_km
                predictions.append(profile)

    if not predictions:
        raise RuntimeError("wrfout中没有读取到预测时刻")

    pred = pd.concat(predictions, ignore_index=True)
    pred = pred.drop_duplicates(subset=["valid_time_utc", "height_m"], keep="last")
    pred = pred.dropna(subset=["wrf_speed"])
    first_model_time = pd.to_datetime(pred["valid_time_utc"], utc=True).min()
    pred["forecast_hour"] = (
        pd.to_datetime(pred["valid_time_utc"], utc=True) - first_model_time
    ).dt.total_seconds() / 3600.0
    if args.spinup_hours > 0:
        pred = pred[pred["forecast_hour"] >= args.spinup_hours].copy()
    return pred, pd.DataFrame(config_rows).drop_duplicates()


def compute_metrics(pairs):
    speed_error = pairs["wrf_speed"] - pairs["obs_speed"]
    direction_error = pairs["dir_diff"].dropna()
    vector_error = np.hypot(pairs["wrf_u"] - pairs["obs_u"], pairs["wrf_v"] - pairs["obs_v"]).dropna()
    return {
        "count": int(len(pairs)),
        "speed_rmse": float(np.sqrt(np.mean(np.square(speed_error)))) if len(pairs) else np.nan,
        "speed_mae": float(np.mean(np.abs(speed_error))) if len(pairs) else np.nan,
        "speed_bias": float(np.mean(speed_error)) if len(pairs) else np.nan,
        "speed_corr": float(pairs["wrf_speed"].corr(pairs["obs_speed"])) if len(pairs) > 1 else np.nan,
        "direction_count": int(len(direction_error)),
        "direction_mae_deg": float(direction_error.abs().mean()) if len(direction_error) else np.nan,
        "direction_bias_deg": float(direction_error.mean()) if len(direction_error) else np.nan,
        "vector_rmse": float(np.sqrt(np.mean(np.square(vector_error)))) if len(vector_error) else np.nan,
    }


def grouped_metrics(pairs, columns):
    rows = []
    for keys, group in pairs.groupby(columns, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = dict(zip(columns, keys))
        row.update(compute_metrics(group))
        rows.append(row)
    return pd.DataFrame(rows)


def add_observation_vectors(pairs):
    result = pairs.copy()
    result["obs_u"], result["obs_v"] = meteorological_from_to_uv(
        result["obs_speed"], result["obs_dir"]
    )
    result["speed_diff"] = result["wrf_speed"] - result["obs_speed"]
    result["dir_diff"] = circular_direction_diff(result["wrf_dir"], result["obs_dir"])
    result["abs_dir_diff"] = result["dir_diff"].abs()
    return result


def direction_hypothesis_metrics(model_u, model_v, obs_speed, obs_direction):
    model_direction = wind_direction(model_u, model_v)
    obs_u, obs_v = meteorological_from_to_uv(obs_speed, obs_direction)
    direction_error = circular_direction_diff(model_direction, obs_direction)
    vector_error = np.hypot(model_u - obs_u, model_v - obs_v)
    valid_direction = np.isfinite(direction_error)
    valid_vector = np.isfinite(vector_error)
    return {
        "count": int(valid_direction.sum()),
        "direction_mae_deg": float(np.nanmean(np.abs(direction_error))) if valid_direction.any() else np.nan,
        "direction_bias_deg": float(np.nanmean(direction_error)) if valid_direction.any() else np.nan,
        "vector_rmse": float(np.sqrt(np.nanmean(np.square(vector_error)))) if valid_vector.any() else np.nan,
    }


def build_direction_diagnostics(pairs):
    model_cases = {
        "earth_rotated": (pairs["wrf_u"], pairs["wrf_v"]),
        "earth_rotated_plus_90deg": (pairs["wrf_v"], -pairs["wrf_u"]),
        "earth_rotated_minus_90deg": (-pairs["wrf_v"], pairs["wrf_u"]),
        "earth_rotated_reversed_180deg": (-pairs["wrf_u"], -pairs["wrf_v"]),
    }
    if {"wrf_grid_u", "wrf_grid_v"}.issubset(pairs.columns):
        model_cases["grid_relative_no_rotation"] = (pairs["wrf_grid_u"], pairs["wrf_grid_v"])

    rows = []
    raw_direction = pairs["obs_dir_raw"]
    for model_case, (model_u, model_v) in model_cases.items():
        for convention in OBS_DIRECTION_CONVENTIONS:
            obs_direction = convert_direction_to_meteorological_from(raw_direction, convention)
            row = {
                "model_component_hypothesis": model_case,
                "observation_direction_convention": convention,
            }
            row.update(
                direction_hypothesis_metrics(
                    np.asarray(model_u, dtype=float),
                    np.asarray(model_v, dtype=float),
                    np.asarray(pairs["obs_speed"], dtype=float),
                    obs_direction,
                )
            )
            rows.append(row)
    return pd.DataFrame(rows).sort_values(["direction_mae_deg", "vector_rmse"], ignore_index=True)


def plot_by_height(pairs, output_dir, max_plots=None):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit("绘图需要 matplotlib：conda install -c conda-forge matplotlib") from exc

    plot_dir = Path(output_dir, "plots_by_height")
    plot_dir.mkdir(parents=True, exist_ok=True)
    groups = sorted(pairs.groupby("height_m"), key=lambda item: item[0])
    if max_plots is not None:
        groups = groups[:max_plots]

    for height, frame in groups:
        frame = frame.sort_values("valid_time_utc")
        times = pd.to_datetime(frame["valid_time_utc"], utc=True).dt.tz_convert(BEIJING_TZ).dt.tz_localize(None)
        fig, axis = plt.subplots(figsize=(11, 4.8), dpi=150)
        axis.plot(times, frame["wrf_speed"], marker="o", label="WRF")
        axis.plot(times, frame["obs_speed"], marker="s", label="Observed")
        axis.set(title=f"Wind Speed at {height:g} m", xlabel="Beijing Time", ylabel="Wind Speed (m/s)")
        axis.grid(alpha=0.3)
        axis.legend()
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(plot_dir / f"wrf_obs_wind_{height:g}m.png")
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="验证WRF风速、风向与测风激光雷达实测数据之间的误差。")
    parser.add_argument("--wrf-dir", default="/root/pyWRF-automation/WRF/test/em_real")
    parser.add_argument("--domain", type=int, default=2)
    parser.add_argument("--obs-dir", default="/root/pyWRF-automation")
    parser.add_argument(
        "--obs-format",
        choices=["auto", "new", "old"],
        default="new",
        help="默认new，以读取含30-120m低空层的KC-WL-3D实测数据",
    )
    parser.add_argument("--output-dir", default="/root/pyWRF-automation/data/wrf_wind_validation")
    parser.add_argument("--lat", type=float, default=31.0, help="实测站纬度")
    parser.add_argument("--lon", type=float, default=118.5, help="实测站经度")
    parser.add_argument("--neighborhood-radius", type=int, default=0, help="0=最近格点；1=最近格点周围3x3平均")
    parser.add_argument("--max-grid-distance-km", type=float, default=30.0)
    parser.add_argument("--spinup-hours", type=float, default=0.0, help="排除模拟开始后的spin-up小时数")
    parser.add_argument(
        "--obs-aggregation",
        choices=["previous_window_mean", "hourly_mean", "exact_hour"],
        default="previous_window_mean",
    )
    parser.add_argument("--obs-window-hours", type=float, default=1.0)
    parser.add_argument("--obs-speed-scale", type=float, default=1.0)
    parser.add_argument(
        "--obs-direction-convention",
        choices=OBS_DIRECTION_CONVENTIONS,
        default="meteorological_from",
        help="观测原始风向定义；默认按气象学来向（正北0度、顺时针）处理",
    )
    parser.add_argument("--core-min-height", type=float, default=DEFAULT_CORE_MIN_HEIGHT_M)
    parser.add_argument("--core-max-height", type=float, default=DEFAULT_CORE_MAX_HEIGHT_M)
    parser.add_argument("--core-height-step", type=float, default=DEFAULT_CORE_HEIGHT_STEP_M)
    parser.add_argument(
        "--all-observation-heights",
        action="store_true",
        help="评估实测中的全部高度；默认评估实际存在的30-120m低空核心层数据",
    )
    parser.add_argument(
        "--require-complete-core-heights",
        action="store_true",
        help="要求30-120m每个核心高度都有实测；默认跳过缺失高度和时刻",
    )
    parser.add_argument("--plots", action="store_true")
    parser.add_argument("--max-plots", type=int, default=None)
    args = parser.parse_args()

    if args.domain < 1 or args.neighborhood_radius < 0:
        raise SystemExit("domain必须大于0，neighborhood-radius不能小于0")

    files = find_wrfout_files(args.wrf_dir, args.domain)
    if not files:
        raise SystemExit(f"没有找到 wrfout_d{args.domain:02d}_*：{args.wrf_dir}")

    with open_wrf_dataset(files[0]) as first_dataset:
        wrf_times = read_wrf_times(first_dataset)
    if not wrf_times:
        raise SystemExit("wrfout中没有Times记录")

    start_bj = wrf_times[0].tz_convert(BEIJING_TZ)
    with open_wrf_dataset(files[-1]) as last_dataset:
        end_bj = read_wrf_times(last_dataset)[-1].tz_convert(BEIJING_TZ)
    obs = load_observations_for_range(
        args.obs_dir, start_bj.strftime("%Y%m%d"), end_bj.strftime("%Y%m%d"), args.obs_format
    )
    if obs.empty:
        raise SystemExit("没有读取到实测数据，请检查 --obs-dir 和 --obs-format")
    obs["obs_speed"] = obs["obs_speed"] * args.obs_speed_scale
    if args.all_observation_heights:
        obs_heights = sorted(obs.loc[obs["height_m"] > 0, "height_m"].dropna().unique())
    else:
        try:
            obs_heights = build_core_heights(
                args.core_min_height, args.core_max_height, args.core_height_step
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        obs = select_core_observations(
            obs, obs_heights, require_complete=args.require_complete_core_heights
        )

    predictions, config = read_wrf_predictions(args, obs_heights)
    target_times = sorted(pd.to_datetime(predictions["valid_time_utc"], utc=True).unique())
    mode = "previous_3h_mean" if args.obs_aggregation == "previous_window_mean" else args.obs_aggregation
    obs_match = aggregate_observations(obs, mode, target_times, args.obs_window_hours)
    obs_match["obs_dir_raw"] = obs_match["obs_dir"]
    obs_match["obs_dir"] = convert_direction_to_meteorological_from(
        obs_match["obs_dir_raw"], args.obs_direction_convention
    )
    pairs = predictions.merge(obs_match, on=["valid_time_utc", "height_m"], how="inner")
    pairs = pairs.dropna(subset=["wrf_speed", "obs_speed"])
    if pairs.empty:
        raise SystemExit("WRF预测与实测数据没有匹配的时间和高度")
    if not args.all_observation_heights and args.require_complete_core_heights:
        paired_heights = pairs["height_m"].dropna().astype(float).unique()
        missing_paired_heights = [
            height
            for height in obs_heights
            if not np.any(np.isclose(paired_heights, height, atol=0.1))
        ]
        if missing_paired_heights:
            raise SystemExit(
                "WRF与实测匹配结果未完整覆盖低空核心层。缺失高度: "
                + ", ".join(f"{height:g}m" for height in missing_paired_heights)
                + "。请检查wrfout垂直层、输出时间及实测时间覆盖。"
            )
    pairs = add_observation_vectors(pairs)
    pairs["comparison_date_bj"] = (
        pd.to_datetime(pairs["valid_time_utc"], utc=True)
        .dt.tz_convert(BEIJING_TZ)
        .dt.strftime("%Y-%m-%d")
    )
    direction_diagnostics = build_direction_diagnostics(pairs)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pairs.to_csv(output_dir / "wrf_obs_pairs.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([compute_metrics(pairs)]).to_csv(output_dir / "metrics_summary.csv", index=False)
    grouped_metrics(pairs, ["height_m"]).to_csv(output_dir / "metrics_by_height.csv", index=False)
    grouped_metrics(pairs, ["comparison_date_bj"]).to_csv(output_dir / "metrics_by_date.csv", index=False)
    grouped_metrics(pairs, ["valid_time_utc"]).to_csv(output_dir / "metrics_by_time.csv", index=False)
    grouped_metrics(pairs, ["forecast_hour"]).to_csv(output_dir / "metrics_by_forecast_hour.csv", index=False)
    config.to_csv(output_dir / "wrf_grid_match.csv", index=False)
    config.to_csv(output_dir / "uv_wind_processing_audit.csv", index=False, encoding="utf-8-sig")
    direction_diagnostics.to_csv(
        output_dir / "direction_convention_diagnostics.csv", index=False, encoding="utf-8-sig"
    )
    (
        pairs.groupby("comparison_date_bj", as_index=False)
        .agg(
            matched_rows=("obs_speed", "count"),
            matched_times=("valid_time_utc", "nunique"),
            matched_heights=("height_m", "nunique"),
            min_height_m=("height_m", "min"),
            max_height_m=("height_m", "max"),
        )
        .to_csv(output_dir / "comparison_coverage_by_date.csv", index=False)
    )
    if not args.all_observation_heights:
        pairs.to_csv(output_dir / "wrf_obs_pairs_core_30_120m.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame([compute_metrics(pairs)]).to_csv(
            output_dir / "metrics_core_30_120m_summary.csv", index=False
        )
        grouped_metrics(pairs, ["height_m"]).to_csv(
            output_dir / "metrics_core_30_120m_by_height.csv", index=False
        )
    if args.plots:
        plot_by_height(pairs, output_dir, args.max_plots)

    print(f"[OUTPUT] {output_dir}")
    print(
        "Validation heights: "
        + ("all observed heights" if args.all_observation_heights else f"{obs_heights[0]:g}-{obs_heights[-1]:g}m")
    )
    print(f"Matched rows: {len(pairs)}")
    print(pd.DataFrame([compute_metrics(pairs)]).to_string(index=False))
    print(config[["nearest_lat", "nearest_lon", "nearest_distance_km", "neighborhood_cell_count"]].head(1).to_string(index=False))
    print("\nBest direction hypotheses (diagnostic only):")
    print(direction_diagnostics.head(8).to_string(index=False))


if __name__ == "__main__":
    main()
