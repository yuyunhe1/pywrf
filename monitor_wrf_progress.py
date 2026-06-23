#!/usr/bin/env python3
import argparse
import csv
import re
import time
from datetime import datetime
from pathlib import Path

import utils


TERMINAL_DAILY_STATUSES = {
    "completed",
    "failed",
    "skipped_existing",
    "not_started_wall_limit",
}


def read_first_namelist_value(text, name):
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*([^,\n]+)", text, re.MULTILINE)
    if not match:
        raise ValueError(f"Could not find {name} in namelist.input")
    return int(match.group(1).strip())


def read_simulation_times(namelist_path):
    text = Path(namelist_path).read_text(errors="replace")
    start = datetime(
        read_first_namelist_value(text, "start_year"),
        read_first_namelist_value(text, "start_month"),
        read_first_namelist_value(text, "start_day"),
        read_first_namelist_value(text, "start_hour"),
    )
    end = datetime(
        read_first_namelist_value(text, "end_year"),
        read_first_namelist_value(text, "end_month"),
        read_first_namelist_value(text, "end_day"),
        read_first_namelist_value(text, "end_hour"),
    )
    return start, end


def read_csv_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8-sig") as file:
            return list(csv.DictReader(file))
    except (OSError, csv.Error):
        return []


def as_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes"}


def progress_bar(fraction, width=30):
    fraction = min(1.0, max(0.0, fraction))
    filled = min(width, int(fraction * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def current_wrf_fraction(wrf_dir):
    try:
        start, end = read_simulation_times(Path(wrf_dir, "namelist.input"))
    except (OSError, ValueError):
        return 0.0, None, None, None

    model_time = utils.latest_wrf_model_time(Path(wrf_dir, "rsl.error.0000"))
    if model_time is None or not start <= model_time <= end:
        return 0.0, start, end, None
    total = max(1.0, (end - start).total_seconds())
    fraction = min(1.0, max(0.0, (model_time - start).total_seconds() / total))
    return fraction, start, end, model_time


def batch_snapshot(base_dir, wrf_dir):
    plan_path = Path(base_dir, "data", "wrf_batch_plan.csv")
    status_path = Path(base_dir, "data", "wrf_batch_status.csv")
    selected = [row["date"] for row in read_csv_rows(plan_path) if as_bool(row.get("selected"))]
    status_rows = read_csv_rows(status_path)

    daily_status = {}
    validation_status = None
    for row in status_rows:
        if row.get("date") == "VALIDATION":
            validation_status = row
        else:
            daily_status[row.get("date", "")] = row

    completed = sum(row.get("status") == "completed" for row in daily_status.values())
    failed = sum(row.get("status") == "failed" for row in daily_status.values())
    skipped = sum(row.get("status") == "skipped_existing" for row in daily_status.values())
    wall_limited = sum(row.get("status") == "not_started_wall_limit" for row in daily_status.values())
    terminal = sum(
        row.get("status") in TERMINAL_DAILY_STATUSES for row in daily_status.values()
    )
    running_dates = [
        date for date, row in daily_status.items() if row.get("status") == "running"
    ]
    current_date = running_dates[-1] if running_dates else None

    current_fraction = 0.0
    current_line = "Current: waiting for the first selected date to start"
    if current_date:
        current_fraction, start, end, model_time = current_wrf_fraction(wrf_dir)
        if start is not None and start.strftime("%Y-%m-%d") != current_date:
            current_fraction = 0.0
            model_time = None
        rsl_path = Path(wrf_dir, "rsl.error.0000")
        rsl_text = rsl_path.read_text(errors="replace") if rsl_path.exists() else ""
        if model_time is not None and "FATAL CALLED" in rsl_text:
            stage = "WRF fatal error; waiting for batch status update"
        elif model_time is not None and "SUCCESS COMPLETE WRF" in rsl_text:
            stage = "WRF complete; saving output"
            current_fraction = 1.0
        elif model_time is not None:
            stage = f"WRF model={model_time:%Y-%m-%d_%H:%M:%S}"
        else:
            stage = "preparing WPS/real.exe or waiting for current rsl.error.0000"
        current_line = (
            f"Current {current_date}: {progress_bar(current_fraction, 24)} "
            f"{current_fraction * 100:6.2f}% {stage}"
        )

    total = len(selected)
    overall_fraction = (terminal + current_fraction) / total if total else 0.0
    remaining = max(0, total - terminal - (1 if current_date else 0))
    elapsed_samples = []
    for row in daily_status.values():
        if row.get("status") not in {"completed", "failed"}:
            continue
        try:
            elapsed = float(row.get("elapsed_hours", ""))
        except ValueError:
            continue
        if elapsed > 0:
            elapsed_samples.append(elapsed)
    average_hours = sum(elapsed_samples) / len(elapsed_samples) if elapsed_samples else None
    eta_text = "calculating"
    if average_hours is not None:
        equivalent_remaining = remaining + (1.0 - current_fraction if current_date else 0.0)
        eta_text = utils.format_duration(average_hours * equivalent_remaining * 3600)

    lines = [
        (
            f"Batch {progress_bar(overall_fraction)} {overall_fraction * 100:6.2f}% "
            f"processed={terminal}/{total} completed={completed} failed={failed} "
            f"skipped={skipped} remaining={remaining} ETA={eta_text}"
        ),
        current_line,
    ]
    if wall_limited:
        lines.append(f"Wall-time limit stopped {wall_limited} date(s); see {status_path}")
    if validation_status:
        status = validation_status.get("status", "")
        lines.append(f"Validation stage: {status}")
    elif total and terminal >= total:
        lines.append("Daily WRF runs complete; waiting for validation stage")
    elif not selected:
        lines.append(f"No selected dates found yet; waiting for {plan_path}")

    batch_complete = bool(
        validation_status
        and validation_status.get("status") in {"validation_completed", "validation_failed"}
    )
    daily_complete = bool(total and terminal >= total)
    status_age = time.time() - status_path.stat().st_mtime if status_path.exists() else 0.0
    stopped_by_wall_limit = wall_limited > 0
    return (
        lines,
        current_date,
        batch_complete,
        daily_complete,
        stopped_by_wall_limit,
        validation_status is not None,
        status_age,
    )


def render(lines, previous_count):
    if previous_count:
        print(f"\033[{previous_count}F", end="")
    for line in lines:
        print(f"\033[2K\r{line}")
    return len(lines)


def monitor_batch(args):
    previous_count = 0
    try:
        while True:
            (
                lines,
                _current_date,
                batch_complete,
                daily_complete,
                stopped_by_wall_limit,
                validation_present,
                status_age,
            ) = batch_snapshot(args.base_dir, args.wrf_dir)
            previous_count = render(lines, previous_count)

            if batch_complete:
                return
            if (
                (daily_complete or stopped_by_wall_limit)
                and not validation_present
                and status_age > max(10, args.interval * 2)
            ):
                return
            time.sleep(max(1, args.interval))
    except KeyboardInterrupt:
        print()


def monitor_single(args):
    wrf_dir = Path(args.wrf_dir)
    start_date, end_date = read_simulation_times(wrf_dir / "namelist.input")
    wall_start = time.time()
    previous_length = 0

    try:
        while True:
            rsl_error = wrf_dir / "rsl.error.0000"
            content = rsl_error.read_text(errors="replace") if rsl_error.exists() else ""
            text = utils.wrf_progress_text(str(wrf_dir), start_date, end_date, wall_start)
            padding = " " * max(0, previous_length - len(text))
            print(f"\r{text}{padding}", end="", flush=True)
            previous_length = len(text)

            if "SUCCESS COMPLETE WRF" in content:
                print("\nWRF completed successfully.")
                return
            if "FATAL CALLED" in content:
                print("\nWRF stopped with a fatal error. Check rsl.error.0000.")
                raise SystemExit(1)
            time.sleep(max(1, args.interval))
    except KeyboardInterrupt:
        print()


def main():
    parser = argparse.ArgumentParser(description="Monitor a single WRF run or a daily WRF batch.")
    parser.add_argument("--base-dir", default="/root/pyWRF-automation")
    parser.add_argument("--wrf-dir", help="Directory containing namelist.input and rsl.error.0000")
    parser.add_argument("--mode", choices=["auto", "batch", "single"], default="auto")
    parser.add_argument("--interval", type=int, default=5, help="Refresh interval in seconds")
    args = parser.parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be greater than zero")
    if not args.wrf_dir:
        args.wrf_dir = str(Path(args.base_dir, "WRF", "test", "em_real"))

    plan_path = Path(args.base_dir, "data", "wrf_batch_plan.csv")
    status_path = Path(args.base_dir, "data", "wrf_batch_status.csv")
    batch_available = plan_path.exists() or status_path.exists()
    mode = "batch" if args.mode == "batch" or (args.mode == "auto" and batch_available) else "single"
    print(f"Monitoring mode: {mode}")
    if mode == "batch":
        monitor_batch(args)
    else:
        monitor_single(args)


if __name__ == "__main__":
    main()
