import argparse
from pathlib import Path

import pandas as pd


DEFAULT_RESULT_DIR = "./data/gfs_wind_validation_one_day/20251101_20260228"


def safe_height_name(height):
    text = f"{height:g}".replace(".", "p")
    return f"wind_speed_{text}m_date_axis.png"


def read_pairs(result_dir, csv_name):
    path = Path(result_dir) / csv_name
    if not path.exists():
        raise SystemExit(f"未找到结果 CSV: {path}")
    df = pd.read_csv(path)
    required = {"height_m", "gfs_speed", "obs_speed"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"CSV 缺少必要列: {sorted(missing)}")

    if "time_bj" in df.columns:
        time_values = pd.to_datetime(df["time_bj"], errors="coerce")
    elif "valid_time_utc" in df.columns:
        time_values = pd.to_datetime(df["valid_time_utc"], errors="coerce", utc=True).dt.tz_convert("Asia/Shanghai")
    else:
        raise SystemExit("CSV 中没有 time_bj 或 valid_time_utc，无法绘制时间轴。")

    df = df.copy()
    df["plot_time"] = time_values.dt.tz_localize(None)
    df["height_m"] = pd.to_numeric(df["height_m"], errors="coerce")
    df["gfs_speed"] = pd.to_numeric(df["gfs_speed"], errors="coerce")
    df["obs_speed"] = pd.to_numeric(df["obs_speed"], errors="coerce")
    df = df.dropna(subset=["plot_time", "height_m", "gfs_speed", "obs_speed"])
    if df.empty:
        raise SystemExit("CSV 中没有可绘制的有效记录。")
    return df


def plot_by_height(df, output_dir, tick_days, max_plots=None, dpi=160):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    grouped = sorted(df.groupby("height_m"), key=lambda item: item[0])
    if max_plots is not None:
        grouped = grouped[:max_plots]

    locator = mdates.DayLocator(interval=tick_days)
    formatter = mdates.DateFormatter("%Y-%m-%d")

    for height, data in grouped:
        data = data.sort_values("plot_time")
        fig, ax = plt.subplots(figsize=(14, 5.2), dpi=dpi)
        ax.plot(data["plot_time"], data["gfs_speed"], linewidth=1.2, label="GFS")
        ax.plot(data["plot_time"], data["obs_speed"], linewidth=1.2, label="Observed")
        ax.set_title(f"Wind Speed at {height:g} m")
        ax.set_xlabel("Beijing Date")
        ax.set_ylabel("Wind Speed (m/s)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)
        fig.autofmt_xdate(rotation=45, ha="right")
        fig.tight_layout()
        fig.savefig(output_dir / safe_height_name(height))
        plt.close(fig)

    return len(grouped)


def main():
    parser = argparse.ArgumentParser(description="基于已有验证 CSV 重新绘制带日期横坐标的风速时间序列图。")
    parser.add_argument("--result-dir", default=DEFAULT_RESULT_DIR, help="已有验证结果目录")
    parser.add_argument("--csv-name", default="gfs_obs_speed_direction_pairs_all.csv")
    parser.add_argument("--output-dir", default=None, help="输出图片目录；默认 result-dir/plots_by_height_date_axis")
    parser.add_argument("--tick-days", type=int, default=5, help="横坐标每隔多少天标一次日期，默认 5 天")
    parser.add_argument("--max-plots", type=int, default=None, help="最多绘制多少个高度；默认全部")
    parser.add_argument("--dpi", type=int, default=160)
    args = parser.parse_args()

    output_dir = args.output_dir or str(Path(args.result_dir) / "plots_by_height_date_axis")
    df = read_pairs(args.result_dir, args.csv_name)
    count = plot_by_height(df, output_dir, args.tick_days, args.max_plots, args.dpi)

    print("=" * 100)
    print(f"Input CSV: {Path(args.result_dir) / args.csv_name}")
    print(f"Output dir: {output_dir}")
    print(f"Tick interval: {args.tick_days} days")
    print(f"Plots: {count}")


if __name__ == "__main__":
    main()
