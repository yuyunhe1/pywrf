"""Read-only real-WRF benchmarks for the segmented framework; no route DB writes."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]
WORK = ROOT / "data/segmented_planning_tests"
EXAMPLE = ROOT / "data/route_example_20261108_1600"
sys.path.insert(0, str(BACKEND))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.update(GFS_AUTO_DOWNLOAD="0", WRF_CACHE_AUTO_SYNC_INDEX="0", WRF_CACHE_SYNC_ON_STARTUP="0",
                  WRF_CACHE_REMOTE_HOST="", WRF_V3_AUTO_SYNC="0", ROUTE_PLAN_MAX_TEMPORAL_LABELS="0",
                  WRF_CACHE_DIR=str(BACKEND.parent / "data/wrf_platform_cache"))

from app import wrf_cache_provider as wrf
from app.forecast_store import IOStats, wrf_product
from app.flight_replay import build_profile
from app.models import FlightProfileRequest, RoutePlanRequest
from app.planning_control import planning_scope, PlanningTimedOut
from app.temporal_plan_service import compute_temporal_plan
from app.wrf_timeline import ForecastDataset, epoch


def pinned_dataset(cycle, level):
    rows = sorted((r for r in wrf.load_index()["files"] if r["cycle"] == cycle), key=lambda r: r["valid_time"])
    if not rows:
        # The online index rolls forward; archived local files still contain
        # their own cycle/time metadata. Do not substitute a newer cycle.
        import numpy as np
        directory = wrf.cache_dir() / datetime.strptime(cycle, "%Y-%m-%d %H:%M UTC").strftime("%Y%m%d%H")
        for path in sorted(directory.glob("*.npz")):
            with np.load(path, allow_pickle=False) as fields:
                assert str(fields["cycle_utc"].item()) == cycle
                rows.append(dict(path=str(path), cycle=cycle, valid_time=str(fields["valid_time_utc"].item()),
                                 forecast_hour=int(fields["forecast_hour"].item())))
    assert rows, f"No local files for pinned forecast cycle {cycle}"
    stats, frames, hashes = IOStats(), [], []
    for row in rows:
        grid, digest = wrf_product(row, level, stats)
        frames.append((epoch(row["valid_time"]), grid))
        hashes.append((row["valid_time"], digest))
    return ForecastDataset("segmented-" + hashlib.sha256(json.dumps([cycle, level, hashes]).encode()).hexdigest()[:24],
                           cycle, level, tuple(frames), frames[0][0], frames[-1][0],
                           io_stats=stats, source_hashes=tuple(hashes))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["saved", "hard", "original", "short"], default="saved")
    parser.add_argument("--algorithm", default="wa_lpa_star")
    parser.add_argument("--budget", type=float, default=180)
    parser.add_argument("--comparison-seconds", type=int, default=0,
                        help="Isolated benchmark only; production keeps the 7200s enclosing budget")
    parser.add_argument("--fresh-cache", action="store_true")
    args = parser.parse_args()
    os.environ["ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS"] = str(args.comparison_seconds)
    WORK.mkdir(parents=True, exist_ok=True)
    payload = json.loads((EXAMPLE / ("long-request.json" if args.case == "hard" else "north-request.json")).read_text(encoding="utf-8"))
    payload.pop("dataset_id", None)
    payload["planner_type"] = args.algorithm
    cycle = "2026-11-08 00:00 UTC"
    if args.case == "original":
        payload.update(start=[117.17468, 30.91165], end=[118.13049, 30.39657], departure_at="2026-11-04T17:00:00+08:00")
        cycle = "2026-11-04 00:00 UTC"
    if args.case == "short":
        payload["end"] = [payload["start"][0] - .008, payload["start"][1] + .004]
    began = monotonic()
    request = RoutePlanRequest(**payload)
    data = pinned_dataset(cycle, request.level)
    setup_seconds = monotonic() - began
    profile_request = FlightProfileRequest(departure_at=request.departure_at, average_ground_speed_mps=request.average_ground_speed_mps,
                                           wind_shear=request.wind_shear, thresholds=request.thresholds)
    reference = None
    if args.case in {"saved", "hard", "original"}:
        if args.case == "original":
            ref = json.loads((ROOT / "data/cross_time_avoidance_tests/classic-user-case-full-lpa_star.json").read_text(encoding="utf-8"))
            points = ref["points"]
        elif args.case == "saved":
            ref = json.loads((EXAMPLE / "verified-plan.json").read_text(encoding="utf-8"))
            points = ref["points"]
        else:
            ref = json.loads((EXAMPLE / "grid-candidates.json").read_text(encoding="utf-8"))
            points = (ref if isinstance(ref, list) else ref.get("candidates", ref.get("tests")))[6]["points"]
        profile = build_profile(dict(route_id="reference", name="reference", points=points, level=request.level, source="wrf"), profile_request, data)
        reference = dict(allowed=profile["decision"]["status"] == "allowed", decision=profile["decision"],
                         distance_km=profile["analysis"]["total_distance_km"])
        assert reference["allowed"], reference
    if args.fresh_cache:
        from uuid import uuid4
        os.environ["FORECAST_DERIVED_CACHE_DIR"] = str(WORK / "cold-cache" / (args.case + "-" + args.algorithm + "-" + uuid4().hex[:8]))
        started_setup = monotonic()
        data = pinned_dataset(cycle, request.level)
        setup_seconds = monotonic() - started_setup
    updates, progress = [], []
    began = monotonic()
    def publish(value):
        updates.append(dict(seconds=monotonic() - began, distance_km=value["distance_km"], objective_cost=value["objective_cost"]))
        (WORK / f"{args.case}-{args.algorithm}-plan.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"publication": updates[-1]}), flush=True)
    def searching(value):
        progress.append(value)
        if len(progress) % 10 == 0:
            print(json.dumps({"progress": value["statistics"]}, ensure_ascii=False), flush=True)
    result, status = None, "completed"
    try:
        with planning_scope(monotonic() + args.budget, Event(), progress=searching):
            result = compute_temporal_plan(request, data, [117., 29.7, 120.15, 32.4], 150000, publish)
    except PlanningTimedOut:
        status = "test_budget_expired"
    row = dict(case=args.case, algorithm=args.algorithm, setup_seconds=setup_seconds, seconds=monotonic() - began,
               status=status, reference=reference, publications=updates,
               feasible=bool(updates), diagnostics=result.get("planning_diagnostics") if result else (progress[-1]["statistics"] if progress else None),
               source_io=data.io_stats.snapshot(), writes_platform_history=False)
    row.update(benchmark_comparison_seconds=args.comparison_seconds, fresh_cache=args.fresh_cache,
               source_snapshot_directory=os.environ.get("FORECAST_DERIVED_CACHE_DIR"))
    (WORK / f"{args.case}-{args.algorithm}-report.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
