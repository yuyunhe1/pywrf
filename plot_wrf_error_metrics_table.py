"""Generate a compact MAE/RMSE table for every WRF validation height."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from replot_wrf_wind_validation_timeseries import (
    configure_chinese_font,
    filter_large_error_samples,
    load_pairs,
)


DEFAULT_DATA_DIR = Path("data/wrf_wind_validation_20251106_20260228")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制各高度层风速 MAE/RMSE 表格。")
    parser.add_argument(
        "--input-csv",
        type=Path,
        default=DEFAULT_DATA_DIR / "wrf_obs_pairs.csv",
    )
    parser.add_argument(
        "--output-image",
        type=Path,
        default=DEFAULT_DATA_DIR / "wrf_wind_mae_rmse_by_height.png",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=DEFAULT_DATA_DIR / "wrf_wind_mae_rmse_by_height.csv",
    )
    parser.add_argument("--max-removal-ratio", type=float, default=0.03)
    parser.add_argument("--outlier-mad-z", type=float, default=3.5)
    parser.add_argument("--columns", type=int, default=5)
    parser.add_argument("--dpi", type=int, default=220)
    return parser.parse_args()


def calculate_metrics(
    pairs: pd.DataFrame,
    max_removal_ratio: float,
    outlier_mad_z: float,
) -> pd.DataFrame:
    rows: list[dict[str, float | int]] = []
    for height, group in pairs.groupby("height_m", sort=True):
        retained, _, _ = filter_large_error_samples(
            group,
            max_removal_ratio=max_removal_ratio,
            mad_z=outlier_mad_z,
        )
        error = retained["wrf_speed"].to_numpy() - retained["obs_speed"].to_numpy()
        rows.append(
            {
                "height_m": float(height),
                "sample_count": int(error.size),
                "mae_mps": float(np.mean(np.abs(error))),
                "rmse_mps": float(np.sqrt(np.mean(np.square(error)))),
            }
        )
    return pd.DataFrame(rows).sort_values("height_m").reset_index(drop=True)


def format_height(value: float) -> str:
    return f"{int(round(value))}" if np.isclose(value, round(value)) else f"{value:g}"


def draw_table(metrics: pd.DataFrame, output: Path, columns: int, dpi: int) -> None:
    if metrics.empty:
        raise ValueError("没有可绘制的高度层指标")
    if columns < 1:
        raise ValueError("columns 必须大于等于 1")

    configure_chinese_font()
    rows_per_block = int(np.ceil(len(metrics) / columns))
    figure_height = max(5.8, 0.25 * rows_per_block + 1.6)
    fig, axes = plt.subplots(
        1,
        columns,
        figsize=(3.75 * columns, figure_height),
        squeeze=False,
    )
    fig.patch.set_facecolor("#f7f9fc")

    headers = ["高度（m）", "MAE（m/s）", "RMSE（m/s）"]
    for block_index, axis in enumerate(axes[0]):
        axis.axis("off")
        start = block_index * rows_per_block
        block = metrics.iloc[start : start + rows_per_block]
        if block.empty:
            continue

        cell_text = [
            [
                format_height(row.height_m),
                f"{row.mae_mps:.3f}",
                f"{row.rmse_mps:.3f}",
            ]
            for row in block.itertuples(index=False)
        ]
        table = axis.table(
            cellText=cell_text,
            colLabels=headers,
            cellLoc="center",
            colLoc="center",
            loc="upper center",
            colWidths=[0.30, 0.32, 0.38],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(9.2)
        table.scale(1.0, 1.35)

        for (row_index, column_index), cell in table.get_celld().items():
            cell.set_edgecolor("#cad3df")
            cell.set_linewidth(0.6)
            if row_index == 0:
                cell.set_facecolor("#24527a")
                cell.get_text().set_color("white")
                cell.get_text().set_weight("bold")
            else:
                cell.set_facecolor("#ffffff" if row_index % 2 else "#edf3f8")
                if column_index == 0:
                    cell.get_text().set_weight("bold")

    fig.suptitle(
        "各高度层风速误差统计",
        fontsize=17,
        fontweight="bold",
        color="#17324d",
        y=0.975,
    )
    fig.subplots_adjust(left=0.025, right=0.975, top=0.89, bottom=0.035, wspace=0.08)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=dpi, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def main() -> None:
    args = parse_args()
    pairs = load_pairs(args.input_csv)
    metrics = calculate_metrics(
        pairs,
        max_removal_ratio=args.max_removal_ratio,
        outlier_mad_z=args.outlier_mad_z,
    )
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(args.output_csv, index=False, encoding="utf-8-sig")
    draw_table(metrics, args.output_image, columns=args.columns, dpi=args.dpi)
    print(f"height_count={len(metrics)}")
    print(f"image={args.output_image.resolve()}")
    print(f"csv={args.output_csv.resolve()}")


if __name__ == "__main__":
    main()
