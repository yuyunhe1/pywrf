import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd

from compare_gfs_wind_one_day import (
    BEIJING_TZ,
    add_error_columns,
    aggregate_observations,
    compute_metrics,
    compute_unit_sensitivity,
    interpolate_profile,
    load_observations_for_range,
)


GRAVITY = 9.80665
TIME_COORDS = ["valid_time", "time", "datetime"]
PRESSURE_COORDS = ["pressure_level", "level", "isobaricInhPa"]
LAT_COORDS = ["latitude", "lat"]
LON_COORDS = ["longitude", "lon"]
PRESSURE_ALIASES = {
    "u": ["u", "u_component_of_wind"],
    "v": ["v", "v_component_of_wind"],
    "z": ["z", "geopotential"],
}
SINGLE_LEVELS = {
    10.0: (
        ["u10", "10u", "10m_u_component_of_wind"],
        ["v10", "10v", "10m_v_component_of_wind"],
        "10 m AGL",
    ),
    100.0: (
        ["u100", "100u", "100m_u_component_of_wind"],
        ["v100", "100v", "100m_v_component_of_wind"],
        "100 m AGL",
    ),
}
SURFACE_Z_ALIASES = ["z", "geopotential", "orography", "surface_geopotential"]


def first_existing(values, candidates):
    for name in candidates:
        if name in values:
            return name
    return None


def beijing_range_window(start_date, end_date):
    start_bj = pd.Timestamp(start_date, tz=BEIJING_TZ)
    end_bj = pd.Timestamp(end_date, tz=BEIJING_TZ) + pd.Timedelta(days=1)
    return start_bj, end_bj, start_bj.tz_convert("UTC"), end_bj.tz_convert("UTC")


def open_netcdf(path):
    try:
        import xarray as xr
    except ImportError as exc:
        raise SystemExit("读取 ERA5 NetCDF 需要 xarray 和 netCDF4：conda install -c conda-forge xarray netcdf4") from exc
    return xr.open_dataset(path)


def normalize_longitude(value, coordinate):
    values = np.asarray(coordinate.values, dtype=float)
    if np.nanmax(values) > 180:
        return value % 360
    return value


def select_time_and_space(da, ds, args):
    time_name = first_existing(da.coords, TIME_COORDS)
    lat_name = first_existing(da.coords, LAT_COORDS)
    lon_name = first_existing(da.coords, LON_COORDS)

    if time_name:
        end_inclusive = args.valid_end_utc_naive - pd.Timedelta(nanoseconds=1)
        da = da.sel({time_name: slice(args.valid_start_utc_naive, end_inclusive)})

    if args.spatial_aggregation == "nearest":
        selector = {}
        if lat_name:
            selector[lat_name] = args.lat
        if lon_name:
            selector[lon_name] = normalize_longitude(args.lon, ds[lon_name])
        if selector:
            da = da.sel(selector, method="nearest")
    else:
        dims = [name for name in (lat_name, lon_name) if name and name in da.dims]
        if dims:
            da = da.mean(dim=dims, skipna=True)

    extra_dims = [dim for dim in da.dims if dim.lower() in {"expver", "number"}]
    if extra_dims:
        da = da.mean(dim=extra_dims, skipna=True)
    return da


def geopotential_to_height(values, units="", force_geopotential=False):
    values = np.asarray(values, dtype=float)
    units_text = str(units).lower()
    if force_geopotential or "m**2" in units_text or "m2" in units_text or "s**-2" in units_text or "s-2" in units_text:
        return values / GRAVITY
    finite = values[np.isfinite(values)]
    if finite.size and np.nanmedian(np.abs(finite)) > 20000:
        return values / GRAVITY
    return values


def inspect_files(data_dir):
    files = sorted(Path(data_dir).rglob("*.nc"))
    if not files:
        raise SystemExit(f"没有找到 NetCDF 文件: {data_dir}")
    for path in files:
        print("=" * 100)
        print(path)
        try:
            with open_netcdf(path) as ds:
                print(f"dims: {dict(ds.sizes)}")
                print(f"coords: {list(ds.coords)}")
                print(f"variables: {list(ds.data_vars)}")
                for name in ds.data_vars:
                    print(f"  {name}: dims={ds[name].dims}, units={ds[name].attrs.get('units', '')}")
        except Exception as exc:
            print(f"[ERROR] {exc}")


def infer_surface_elevation_from_dataset(ds, args):
    pressure_name = first_existing(ds.coords, PRESSURE_COORDS)
    for name in SURFACE_Z_ALIASES:
        if name not in ds.data_vars:
            continue
        da = ds[name]
        if pressure_name and pressure_name in da.dims:
            continue
        da = select_time_and_space(da, ds, args)
        time_name = first_existing(da.dims, TIME_COORDS)
        if time_name:
            da = da.mean(dim=time_name, skipna=True)
        value = float(np.asarray(da.squeeze()).reshape(-1)[0])
        return float(
            geopotential_to_height(
                [value],
                da.attrs.get("units", ""),
                force_geopotential=name in {"z", "geopotential", "surface_geopotential"},
            )[0]
        )
    return None


def dataarray_to_time_series(da, ds, args):
    da = select_time_and_space(da, ds, args)
    time_name = first_existing(da.dims, TIME_COORDS)
    if not time_name:
        return pd.Series(dtype=float)
    series = da.to_series()
    if isinstance(series.index, pd.MultiIndex):
        other_levels = [name for name in series.index.names if name != time_name]
        if other_levels:
            series = series.groupby(level=time_name).mean()
    series.index = pd.to_datetime(series.index, utc=True)
    return series.sort_index()


def pressure_records_from_dataset(ds, path, args, surface_elevation_m):
    pressure_name = first_existing(ds.coords, PRESSURE_COORDS)
    time_name = first_existing(ds.coords, TIME_COORDS)
    u_name = first_existing(ds.data_vars, PRESSURE_ALIASES["u"])
    v_name = first_existing(ds.data_vars, PRESSURE_ALIASES["v"])
    z_name = first_existing(ds.data_vars, PRESSURE_ALIASES["z"])
    if not all([pressure_name, time_name, u_name, v_name, z_name]):
        return []

    arrays = {}
    for key, name in [("u", u_name), ("v", v_name), ("z", z_name)]:
        arrays[key] = select_time_and_space(ds[name], ds, args)

    import xarray as xr

    frame = xr.Dataset(arrays).to_dataframe().reset_index()
    frame = frame.dropna(subset=["u", "v", "z"])
    if frame.empty:
        return []

    frame["valid_time_utc"] = pd.to_datetime(frame[time_name], utc=True)
    frame["pressure_hpa"] = pd.to_numeric(frame[pressure_name], errors="coerce")
    frame["height_agl_m"] = geopotential_to_height(
        frame["z"],
        ds[z_name].attrs.get("units", ""),
        force_geopotential=z_name in {"z", "geopotential"},
    ) - surface_elevation_m
    frame["source_level"] = frame["pressure_hpa"].map(lambda value: f"{value:g} hPa")
    frame["source_file"] = path.name
    return frame[
        ["valid_time_utc", "pressure_hpa", "height_agl_m", "u", "v", "source_level", "source_file"]
    ].to_dict("records")


def single_level_records_from_dataset(ds, path, args):
    records = []
    for height, (u_aliases, v_aliases, source_level) in SINGLE_LEVELS.items():
        u_name = first_existing(ds.data_vars, u_aliases)
        v_name = first_existing(ds.data_vars, v_aliases)
        if not u_name or not v_name:
            continue
        u_series = dataarray_to_time_series(ds[u_name], ds, args)
        v_series = dataarray_to_time_series(ds[v_name], ds, args)
        joined = pd.concat([u_series.rename("u"), v_series.rename("v")], axis=1).dropna()
        for valid_time, row in joined.iterrows():
            records.append(
                {
                    "valid_time_utc": valid_time,
                    "pressure_hpa": np.nan,
                    "height_agl_m": height,
                    "u": float(row["u"]),
                    "v": float(row["v"]),
                    "source_level": source_level,
                    "source_file": path.name,
                }
            )
    return records


def read_era5_profiles(args):
    files = sorted(Path(args.era5_dir).rglob("*.nc"))
    if not files:
        raise RuntimeError(f"没有找到 ERA5 NetCDF 文件: {args.era5_dir}")

    surface_elevation_m = args.surface_elevation_m
    records = []
    for index, path in enumerate(files, start=1):
        print(f"[ERA5] {index}/{len(files)} {path}")
        with open_netcdf(path) as ds:
            if surface_elevation_m is None:
                value = infer_surface_elevation_from_dataset(ds, args)
                if value is not None and np.isfinite(value):
                    surface_elevation_m = value
                    print(f"[ERA5] 从 {path.name} 推断地表海拔: {surface_elevation_m:.2f} m")
            records.extend(single_level_records_from_dataset(ds, path, args))
            records.extend(pressure_records_from_dataset(ds, path, args, 0.0))

    if not records:
        raise RuntimeError("ERA5 NetCDF 中没有读取到可用的 U/V 风或位势数据。")
    if surface_elevation_m is None:
        raise RuntimeError(
            "ERA5 文件中未找到地表位势，无法把等压层位势高度转换为离地高度。"
            "请重新下载单层 geopotential，或通过 --surface-elevation-m 指定测站海拔。"
        )

    profiles = pd.DataFrame(records)
    pressure_rows = profiles["pressure_hpa"].notna()
    profiles.loc[pressure_rows, "height_agl_m"] = (
        profiles.loc[pressure_rows, "height_agl_m"] - surface_elevation_m
    )
    profiles = profiles[(profiles["pressure_hpa"].isna()) | (profiles["height_agl_m"] > 0)].copy()
    profiles = profiles.drop_duplicates(
        subset=["valid_time_utc", "height_agl_m", "pressure_hpa", "source_level"], keep="last"
    )
    return profiles, surface_elevation_m


def build_era5_predictions(profiles, obs_heights):
    rows = []
    for valid_time, profile in profiles.groupby("valid_time_utc", sort=True):
        pred = interpolate_profile(profile, obs_heights)
        pred["valid_time_utc"] = pd.Timestamp(valid_time)
        pred["era5_spatial_aggregation"] = "prepared"
        rows.append(pred)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def aggregate_observations_once(obs, target_times, window_hours):
    if not np.isclose(window_hours, 1.0):
        return aggregate_observations(obs, "previous_3h_mean", target_times, window_hours)

    work = obs.copy()
    work["valid_time_utc"] = work["time_utc"].dt.ceil("h")
    target_set = {pd.Timestamp(value) for value in target_times}
    work = work[work["valid_time_utc"].isin(target_set)]
    if work.empty:
        return pd.DataFrame()

    speed = (
        work.groupby(["valid_time_utc", "height_m"], as_index=False)
        .agg(obs_speed=("obs_speed", "mean"), sample_count=("obs_speed", "count"))
    )
    direction = (
        work.groupby(["valid_time_utc", "height_m"])["obs_dir"]
        .apply(lambda values: np.rad2deg(np.arctan2(
            np.sin(np.deg2rad(pd.to_numeric(values, errors="coerce").dropna())).mean(),
            np.cos(np.deg2rad(pd.to_numeric(values, errors="coerce").dropna())).mean(),
        )) % 360 if pd.to_numeric(values, errors="coerce").notna().any() else np.nan)
        .reset_index(name="obs_dir")
    )
    result = speed.merge(direction, on=["valid_time_utc", "height_m"], how="left")
    result["time_bj"] = result["valid_time_utc"].dt.tz_convert(BEIJING_TZ)
    result["obs_window_start_utc"] = result["valid_time_utc"] - pd.Timedelta(hours=window_hours)
    result["obs_window_end_utc"] = result["valid_time_utc"]
    return result


def plot_by_height(pairs, output_dir, tick_days, max_plots=None):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    plot_dir = Path(output_dir) / "plots_by_height"
    plot_dir.mkdir(parents=True, exist_ok=True)
    grouped = sorted(pairs.groupby("height_m"), key=lambda item: item[0])
    if max_plots is not None:
        grouped = grouped[:max_plots]

    for height, data in grouped:
        data = data.sort_values("valid_time_utc").copy()
        plot_time = pd.to_datetime(data["valid_time_utc"], utc=True).dt.tz_convert(BEIJING_TZ).dt.tz_localize(None)
        fig, ax = plt.subplots(figsize=(14, 5.2), dpi=160)
        ax.plot(plot_time, data["gfs_speed"], linewidth=1.1, label="ERA5")
        ax.plot(plot_time, data["obs_speed"], linewidth=1.1, label="Observed")
        ax.set_title(f"Wind Speed at {height:g} m")
        ax.set_xlabel("Beijing Date")
        ax.set_ylabel("Wind Speed (m/s)")
        ax.grid(True, alpha=0.3)
        ax.legend()
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=tick_days))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        fig.autofmt_xdate(rotation=45, ha="right")
        fig.tight_layout()
        height_text = f"{height:g}".replace(".", "p")
        filename = f"era5_wind_speed_{height_text}m.png"
        fig.savefig(plot_dir / filename)
        plt.close(fig)
    print(f"[PLOTS] {plot_dir} ({len(grouped)} files)")


def write_outputs(pairs, pred, profiles, surface_elevation_m, args):
    out_dir = Path(args.output_dir) / f"{args.start_date}_{args.end_date}"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = compute_metrics(pairs).to_frame().T
    by_height = pairs.groupby("height_m").apply(compute_metrics, include_groups=False).reset_index()
    by_date = pairs.groupby("compare_date").apply(compute_metrics, include_groups=False).reset_index()
    by_time = pairs.groupby("valid_time_utc").apply(compute_metrics, include_groups=False).reset_index()

    output_pairs = pairs.rename(
        columns={
            "gfs_speed": "era5_speed",
            "gfs_dir": "era5_dir",
            "gfs_u": "era5_u",
            "gfs_v": "era5_v",
            "gfs_lower_height_m": "era5_lower_height_m",
            "gfs_upper_height_m": "era5_upper_height_m",
        }
    )
    output_pairs.to_csv(out_dir / "era5_obs_speed_direction_pairs_all.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out_dir / "metrics_summary_all.csv", index=False, encoding="utf-8-sig")
    by_height.to_csv(out_dir / "metrics_by_height_all.csv", index=False, encoding="utf-8-sig")
    by_date.to_csv(out_dir / "metrics_by_date.csv", index=False, encoding="utf-8-sig")
    by_time.to_csv(out_dir / "metrics_by_time.csv", index=False, encoding="utf-8-sig")
    compute_unit_sensitivity(pairs).to_csv(out_dir / "unit_sensitivity_all.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"surface_elevation_m": surface_elevation_m}]).to_csv(
        out_dir / "era5_validation_config.csv", index=False, encoding="utf-8-sig"
    )

    pred.groupby("height_m", as_index=False).agg(
        era5_count=("gfs_speed", "count"),
        min_era5_speed=("gfs_speed", "min"),
        max_era5_speed=("gfs_speed", "max"),
        vertical_match_methods=("vertical_match_method", lambda values: ";".join(sorted(set(values.dropna().astype(str))))),
        era5_lower_height_m=("gfs_lower_height_m", "min"),
        era5_upper_height_m=("gfs_upper_height_m", "max"),
    ).to_csv(out_dir / "era5_prediction_height_summary_all.csv", index=False, encoding="utf-8-sig")

    if args.save_profiles:
        profiles.to_csv(out_dir / "era5_profile_levels_all.csv", index=False, encoding="utf-8-sig")
    if args.plots:
        plot_by_height(pairs, out_dir, args.tick_days, args.max_plots)

    print("=" * 100)
    print(f"[OUTPUT] {out_dir}")
    print(f"Surface elevation used: {surface_elevation_m:.2f} m")
    print(summary.to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description="一次性验证 ERA5 与测风激光雷达的全时段风速和风向误差。")
    parser.add_argument(
        "--era5-dir",
        default="/root/graph-cast-rain-model/data_download/data/ERA5_Wind_202511_202602",
    )
    parser.add_argument("--obs-dir", default="./")
    parser.add_argument("--obs-format", choices=["auto", "new", "old"], default="old")
    parser.add_argument("--output-dir", default="./data/era5_wind_validation")
    parser.add_argument("--start-date", default="20251101")
    parser.add_argument("--end-date", default="20260228")
    parser.add_argument("--lat", type=float, default=31.12)
    parser.add_argument("--lon", type=float, default=118.66)
    parser.add_argument("--spatial-aggregation", choices=["region_mean", "nearest"], default="region_mean")
    parser.add_argument("--surface-elevation-m", type=float, default=None, help="测站地面海拔；等压层转离地高度需要")
    parser.add_argument("--obs-window-hours", type=float, default=1.0, help="每个 ERA5 小时时刻采用此前多少小时的实测平均")
    parser.add_argument("--obs-speed-scale", type=float, default=1.0)
    parser.add_argument("--plots", action="store_true")
    parser.add_argument("--tick-days", type=int, default=5)
    parser.add_argument("--max-plots", type=int, default=None)
    parser.add_argument("--save-profiles", action="store_true")
    parser.add_argument("--inspect-only", action="store_true", help="只打印 NetCDF 变量、维度和单位")
    args = parser.parse_args()

    if args.inspect_only:
        inspect_files(args.era5_dir)
        return

    _, _, valid_start_utc, valid_end_utc = beijing_range_window(args.start_date, args.end_date)
    args.valid_start_utc_naive = valid_start_utc.tz_localize(None)
    args.valid_end_utc_naive = valid_end_utc.tz_localize(None)

    print("=" * 100)
    print(f"ERA5 dir: {args.era5_dir}")
    print(f"Beijing dates: {args.start_date} -> {args.end_date}")
    print(f"UTC window: {valid_start_utc} -> {valid_end_utc}")
    print(f"Spatial aggregation: {args.spatial_aggregation}")
    print(f"OBS previous mean window: {args.obs_window_hours} hour(s)")

    obs = load_observations_for_range(args.obs_dir, args.start_date, args.end_date, args.obs_format)
    if obs.empty:
        raise SystemExit("没有读取到实测风速。")
    obs_start = valid_start_utc - pd.Timedelta(hours=args.obs_window_hours)
    obs = obs[(obs["time_utc"] >= obs_start) & (obs["time_utc"] <= valid_end_utc)].copy()
    obs_heights = sorted(obs["height_m"].dropna().unique())

    profiles, surface_elevation_m = read_era5_profiles(args)
    pred = build_era5_predictions(profiles, obs_heights)
    if pred.empty:
        raise SystemExit("没有生成 ERA5 风速预测值。")

    target_times = sorted(pred["valid_time_utc"].dropna().unique())
    obs_match = aggregate_observations_once(obs, target_times, args.obs_window_hours)
    pairs = pred.merge(obs_match, on=["valid_time_utc", "height_m"], how="inner")
    pairs = pairs.dropna(subset=["gfs_speed", "obs_speed"])
    if pairs.empty:
        raise SystemExit("ERA5 与实测没有匹配上的时间和高度。")
    pairs = add_error_columns(pairs, args.obs_speed_scale)
    write_outputs(pairs, pred, profiles, surface_elevation_m, args)


if __name__ == "__main__":
    main()
