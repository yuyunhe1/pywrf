"""Plot observed and WRF wind-speed time series for every matched height."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import MaxNLocator
import numpy as np
import pandas as pd


DEFAULT_INPUT_DIR = Path("data/wrf_wind_validation_20251106_20260228")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按高度绘制仅包含 WRF 和实测风速的时间序列对比图。"
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        default=DEFAULT_INPUT_DIR / "wrf_obs_pairs.csv",
        help="WRF 与实测匹配结果 CSV。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR / "plots_by_heights",
        help="图片输出目录。",
    )
    parser.add_argument("--dpi", type=int, default=160)
    parser.add_argument(
        "--max-removal-ratio",
        type=float,
        default=0.03,
        help="每个高度允许剔除的最大样本比例，默认 0.03。",
    )
    parser.add_argument(
        "--outlier-mad-z",
        type=float,
        default=3.5,
        help="明显异常误差的稳健 MAD 倍数，默认 3.5。",
    )
    return parser.parse_args()


def load_pairs(path: Path) -> pd.DataFrame:
    pairs = pd.read_csv(path)
    required = {"height_m", "time_bj", "wrf_speed", "obs_speed"}
    missing = sorted(required.difference(pairs.columns))
    if missing:
        raise ValueError(f"输入 CSV 缺少字段: {', '.join(missing)}")

    for column in ("height_m", "wrf_speed", "obs_speed"):
        pairs[column] = pd.to_numeric(pairs[column], errors="coerce")
    pairs["time_bj"] = pd.to_datetime(pairs["time_bj"], errors="coerce", utc=True)
    pairs["time_bj"] = pairs["time_bj"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    pairs = pairs.dropna(subset=["height_m", "time_bj", "wrf_speed", "obs_speed"])
    pairs = pairs[np.isfinite(pairs[["height_m", "wrf_speed", "obs_speed"]]).all(axis=1)]
    return pairs.sort_values(["height_m", "time_bj"]).reset_index(drop=True)


def configure_chinese_font() -> str:
    available = {item.name for item in font_manager.fontManager.ttflist}
    candidates = (
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Source Han Sans CN",
        "Arial Unicode MS",
    )
    selected = next((name for name in candidates if name in available), "DejaVu Sans")
    plt.rcParams["font.sans-serif"] = [selected, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return selected


def height_label(height: float) -> str:
    return str(int(round(height))) if np.isclose(height, round(height)) else f"{height:g}"


def filter_large_error_samples(
    frame: pd.DataFrame,
    max_removal_ratio: float,
    mad_z: float,
) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Remove only robust-error outliers, capped strictly below the ratio."""

    if not 0 <= max_removal_ratio <= 0.03:
        raise ValueError("max_removal_ratio 必须位于 0 到 0.03 之间")
    if not np.isfinite(mad_z) or mad_z <= 0:
        raise ValueError("outlier_mad_z 必须是正数")

    work = frame.copy()
    work["absolute_error"] = (work["wrf_speed"] - work["obs_speed"]).abs()
    errors = work["absolute_error"].to_numpy(dtype=float)
    median_error = float(np.median(errors))
    mad = float(np.median(np.abs(errors - median_error)))
    robust_sigma = 1.4826 * mad
    if robust_sigma > np.finfo(float).eps:
        threshold = median_error + mad_z * robust_sigma
    else:
        q1, q3 = np.quantile(errors, [0.25, 0.75])
        threshold = float(q3 + 3.0 * max(q3 - q1, np.finfo(float).eps))

    max_remove = int(np.floor(len(work) * max_removal_ratio + 1e-12))
    candidates = work[work["absolute_error"] > threshold].sort_values("absolute_error", ascending=False)
    removed = candidates.head(max_remove) if max_remove > 0 else candidates.head(0)
    filtered = work.drop(index=removed.index).sort_values("time_bj").reset_index(drop=True)
    return filtered, removed.sort_values("time_bj").reset_index(drop=True), float(threshold)


def plot_height(
    frame: pd.DataFrame,
    height: float,
    output_dir: Path,
    dpi: int,
) -> Path:
    display = frame.sort_values("time_bj").reset_index(drop=True)
    label = height_label(height)
    output_path = output_dir / f"wind_speed_height_{label}m.png"
    x = np.arange(len(display), dtype=float)
    observed = display["obs_speed"].to_numpy(dtype=float)
    wrf = display["wrf_speed"].to_numpy(dtype=float)

    fig, ax = plt.subplots(figsize=(12.5, 4.8), constrained_layout=True)
    ax.fill_between(
        x,
        observed,
        wrf,
        color="#ff9800",
        alpha=0.14,
        linewidth=0,
        zorder=1,
    )
    ax.plot(
        x,
        observed,
        color="#202124",
        linewidth=1.65,
        marker="o",
        markersize=2.1,
        label="实测风速",
        zorder=3,
    )
    ax.plot(
        x,
        wrf,
        color="#e53935",
        linewidth=1.65,
        marker="o",
        markersize=2.1,
        label="WRF降尺度风速",
        zorder=2,
    )

    all_values = np.concatenate((observed, wrf))
    value_min = float(np.nanmin(all_values))
    value_max = float(np.nanmax(all_values))
    value_span = max(value_max - value_min, 1.0)
    ax.set_ylim(max(0.0, value_min - value_span * 0.05), value_max + value_span * 0.08)
    ax.set_xlim(0, max(len(display) - 1, 1))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=7))

    tick_count = min(10, len(display))
    tick_indices = np.unique(np.linspace(0, len(display) - 1, tick_count, dtype=int))
    tick_labels = [display.loc[index, "time_bj"].strftime("%m-%d\n%H:%M") for index in tick_indices]
    ax.set_xticks(tick_indices)
    ax.set_xticklabels(tick_labels)

    mae = float(np.mean(np.abs(wrf - observed)))
    rmse = float(np.sqrt(np.mean((wrf - observed) ** 2)))
    ax.set_title(f"{label}米高度实测与WRF降尺度风速对比", fontsize=14, pad=9)
    ax.set_xlabel("WRF可用时刻（北京时间 UTC+8，缺测时段已压缩）", fontsize=10.5)
    ax.set_ylabel("风速（m/s）", fontsize=11)
    ax.grid(True, color="#9aa0a6", alpha=0.25, linewidth=0.8)
    ax.legend(loc="upper right", frameon=True, framealpha=0.95, fontsize=9.5)
    ax.text(
        0.012,
        0.975,
        f"MAE：{mae:.2f} m/s    RMSE：{rmse:.2f} m/s",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=9,
        color="#37474f",
        bbox={"boxstyle": "round,pad=0.3", "facecolor": "white", "edgecolor": "#cfd8dc", "alpha": 0.88},
    )
    ax.tick_params(axis="both", labelsize=9)
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)
    return output_path


def main() -> None:
    args = parse_args()
    selected_font = configure_chinese_font()
    input_csv = args.input_csv.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    pairs = load_pairs(input_csv)
    if pairs.empty:
        raise SystemExit(f"没有可绘制的 WRF/实测匹配记录: {input_csv}")

    manifest: list[dict] = []
    removed_records: list[pd.DataFrame] = []
    for height, group in pairs.groupby("height_m", sort=True):
        group = group.sort_values("time_bj")
        filtered, removed, error_threshold = filter_large_error_samples(
            group,
            args.max_removal_ratio,
            args.outlier_mad_z,
        )
        output_path = plot_height(
            filtered,
            float(height),
            output_dir,
            args.dpi,
        )
        if not removed.empty:
            audit = removed[["time_bj", "obs_speed", "wrf_speed", "absolute_error"]].copy()
            audit.insert(0, "height_m", float(height))
            audit["absolute_error_threshold_mps"] = error_threshold
            removed_records.append(audit)
        manifest.append(
            {
                "height_m": float(height),
                "original_point_count": int(len(group)),
                "retained_point_count": int(len(filtered)),
                "removed_point_count": int(len(removed)),
                "removed_ratio": round(len(removed) / len(group), 6),
                "absolute_error_threshold_mps": round(error_threshold, 6),
                "max_removal_ratio": args.max_removal_ratio,
                "outlier_mad_z": args.outlier_mad_z,
                "start_time_bj": filtered["time_bj"].min().isoformat(sep=" "),
                "end_time_bj": filtered["time_bj"].max().isoformat(sep=" "),
                "file_name": output_path.name,
                "series": "observed_speed,wrf_speed",
            }
        )

    manifest_path = output_dir / "plot_manifest.csv"
    pd.DataFrame(manifest).to_csv(manifest_path, index=False, encoding="utf-8-sig")
    removed_path = output_dir / "removed_samples.csv"
    removed_columns = [
        "height_m",
        "time_bj",
        "obs_speed",
        "wrf_speed",
        "absolute_error",
        "absolute_error_threshold_mps",
    ]
    removed_table = pd.concat(removed_records, ignore_index=True) if removed_records else pd.DataFrame(columns=removed_columns)
    removed_table.to_csv(removed_path, index=False, encoding="utf-8-sig")
    print(f"Generated {len(manifest)} height plots in: {output_dir}")
    print(f"Manifest: {manifest_path}")
    print(f"Removed-sample audit: {removed_path}")
    print(f"Chinese font: {selected_font}")
    total_original = sum(item["original_point_count"] for item in manifest)
    total_removed = sum(item["removed_point_count"] for item in manifest)
    max_ratio = max(item["removed_ratio"] for item in manifest)
    print(
        f"Removed {total_removed}/{total_original} samples "
        f"({total_removed / total_original:.2%}); maximum per-height ratio: {max_ratio:.2%}"
    )


if __name__ == "__main__":
    main()
