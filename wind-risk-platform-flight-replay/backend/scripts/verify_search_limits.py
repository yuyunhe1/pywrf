"""Read-only historical WRF checks; never writes platform route records."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic

BACKEND = Path(__file__).resolve().parents[1]
WORK = BACKEND.parents[1] / "data/search_limits_tests"
EXAMPLE = BACKEND.parents[1] / "data/route_example_20261108_1600"
sys.path.insert(0, str(BACKEND))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.update(GFS_AUTO_DOWNLOAD="0", WRF_CACHE_AUTO_SYNC_INDEX="0", WRF_CACHE_SYNC_ON_STARTUP="0",
                  WRF_CACHE_REMOTE_HOST="", WRF_V3_AUTO_SYNC="0",
                  WRF_CACHE_DIR=str(BACKEND.parent / "data/wrf_platform_cache"),
                  ROUTE_PLAN_MAX_TEMPORAL_LABELS="0")

from app.flight_replay import build_profile
from app.models import RoutePlanRequest, FlightProfileRequest
from app.planning_control import planning_scope, PlanningTimedOut
from app.planning_limits import available_memory_mb
from app.temporal_plan_service import compute_temporal_plan
from app.temporal_routing import TemporalPlanner
from app import wrf_cache_provider as wrf
from app.wrf_timeline import ForecastDataset, epoch


def original_dataset(level):
    # Newer cycles may cover the historical departure too. Select the actual
    # 00 UTC files used by the original report, rather than the newest forecast.
    cycle = "2026-11-08 00:00 UTC"
    records = sorted((row for row in wrf.load_index()["files"] if row["cycle"] == cycle),
                     key=lambda row: row["valid_time"])
    frames = tuple((epoch(row["valid_time"]), wrf.get_local_grid(row, level)) for row in records)
    assert len(frames) == 48, len(frames)
    return ForecastDataset("historical_wrf_2026110800_search_limits", cycle, level, frames,
                           frames[0][0], frames[-1][0])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=float, default=180)
    parser.add_argument("--case", choices=["all", "hard", "saved"], default="all")
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    report = {"tests": [], "writes_platform_history": False}
    report_path = WORK / "real-wrf-report.json"
    if args.case != "all" and report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    for name, request_name, budget in [("previously_resource_limited", "long-request.json", args.budget),
                                       ("saved_85km_regression", "north-request.json", min(45, args.budget))]:
        if args.case == "hard" and name != "previously_resource_limited" or args.case == "saved" and name != "saved_85km_regression":
            continue
        payload = json.loads((EXAMPLE / request_name).read_text(encoding="utf-8"))
        payload.pop("dataset_id", None)
        request = RoutePlanRequest(**payload)
        data = original_dataset(request.level)
        assert data.cycle == "2026-11-08 00:00 UTC", data.cycle
        updates, searches = [], []
        original_search = TemporalPlanner.search
        def measured(self, *a, **kw):
            began = monotonic()
            try:
                result = original_search(self, *a, **kw)
                searches.append({"status": "returned", "arrival_labels": result["arrival_labels"],
                                 "expanded_nodes": result["expanded_nodes"], "seconds": monotonic() - began})
                return result
            except Exception as exc:
                searches.append({"status": type(exc).__name__, "message": str(exc),
                                 **getattr(self, "last_search_diagnostics", {}),
                                 **getattr(exc, "diagnostics", {}), "seconds": monotonic() - began})
                raise
        TemporalPlanner.search = measured
        began = monotonic()
        def publish(result):
            assert result["analysis"]["navigation_allowed"] and not result["reference_route"]
            updates.append(deepcopy(result))
            print(json.dumps({"case": name, "published_at": monotonic() - began,
                              "distance_km": result["distance_km"], "objective_cost": result["objective_cost"]}), flush=True)
        status, result = "completed", None
        print(json.dumps({"case": name, "starting": True, "dataset_id": data.dataset_id,
                          "budget": budget, "available_memory_mb": available_memory_mb()}), flush=True)
        try:
            with planning_scope(monotonic() + budget, Event()):
                result = compute_temporal_plan(request, data, [117., 29.7, 120.15, 32.4], 150000, publish)
        except PlanningTimedOut:
            status = "test_budget_expired"
            result = updates[-1] if updates else None
        finally:
            TemporalPlanner.search = original_search
        feasible = bool(result and result.get("analysis", {}).get("navigation_allowed"))
        if feasible:
            profile = build_profile({"route_id": "search-limits-regression", "name": name,
                                     "points": result["points"], "source": "wrf", "level": request.level},
                                    FlightProfileRequest(departure_at=request.departure_at,
                                                         average_ground_speed_mps=10), data)
            assert profile["decision"]["status"] == "allowed"
            (WORK / (name + "-plan.json")).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        if name == "saved_85km_regression":
            assert feasible, "Previously flyable route must still produce a verified result"
        row = {"case": name, "status": status, "seconds": monotonic() - began,
               "feasible": feasible, "distance_km": result.get("distance_km") if result else None,
               "first_feasible_seconds": result.get("planning_diagnostics", {}).get("first_feasible_seconds") if result else None,
               "route_status": result.get("route_status") if result else None,
               "searches": searches, "available_memory_after_mb": available_memory_mb()}
        report["tests"] = [item for item in report["tests"] if item["case"] != name] + [row]
        (WORK / "real-wrf-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
