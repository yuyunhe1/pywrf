"""Real 2026-11-04 17:00 Beijing regression; no saved-route mutations."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.update(WRF_CACHE_AUTO_SYNC_INDEX="0", WRF_CACHE_SYNC_ON_STARTUP="0", GFS_AUTO_DOWNLOAD="0",
                  WRF_CACHE_REMOTE_HOST="", WRF_V3_AUTO_SYNC="0")
from app import v3_service, v3_native
from app.main import compute_route_plan, route_planning_bbox
from app.models import RoutePlanRequest
from app.planning_control import planning_scope
from app.temporal_plan_service import compute_temporal_plan
from app.wrf_timeline import load_dataset

WORK = BACKEND.parents[1] / "data/cross_time_avoidance_tests"
START, END = (117.17468, 30.91165), (118.13049, 30.39657)
AT = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


class First(Exception):
    def __init__(self, result):
        self.result = result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--v3", action="store_true")
    parser.add_argument("--short", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--budget", type=float, default=600)
    parser.add_argument("--altitude", type=float)
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    start, end = ((117.6, 31.), (117.61, 31.)) if args.short else (START, END)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "departure_at": AT.isoformat(),
              "start": start, "end": end, "cases": []}
    name = ("v3" if args.v3 else "classic") + ("-short" if args.short else "-user-case") + ("-full" if args.full else "-first")
    algorithms = ("wrf_v3",) if args.v3 else ("astar", "lpa_star", "wa_lpa_star")
    for algorithm in algorithms:
        for altitude in ((args.altitude,) if args.altitude else (300, 500, 800) if args.v3 else (100,)):
            request = RoutePlanRequest(start=start, end=end, source="wrf", level="100m AGL", time_aware=True,
                                       departure_at=AT, planner_type=algorithm, planning_strategy="wind_avoidance",
                                       altitude_msl_m=altitude)
            if args.v3:
                request, dataset = v3_service.prepare_plan(request)
            else:
                dataset = load_dataset(request.level, AT)
            began = monotonic()
            updates = []
            def publish(result):
                updates.append({"seconds": monotonic() - began, "objective_cost": result["objective_cost"],
                                "points": len(result["points"]), "search_phase": result["search_phase"]})
                print(json.dumps({"algorithm": algorithm, "update": updates[-1]}, ensure_ascii=False), flush=True)
                if not args.full:
                    raise First(result)
            try:
                with planning_scope(monotonic() + args.budget, Event()):
                    if args.v3:
                        result = v3_service.compute_plan(request, publish, dataset)
                    else:
                        bbox = list(map(float, route_planning_bbox(start, end, "wind_avoidance").split(",")))
                        result = compute_temporal_plan(request, dataset, bbox, 20000, publish)
            except First as found:
                result = found.result
            except Exception as exc:
                print(json.dumps({"algorithm": algorithm, "altitude": altitude, "error": str(exc)}, ensure_ascii=False), flush=True)
                if args.v3 and "BELOW_TERRAIN" in str(exc):
                    continue
                raise
            assert result["analysis"]["navigation_allowed"] and not result["reference_route"]
            assert updates and all(a["objective_cost"] > b["objective_cost"] for a, b in zip(updates, updates[1:]))
            if not args.short:
                assert len(result["points"]) > 2
            if args.v3:
                assert not result["planning_activity"]["execution_modules_loaded"]
                assert result["endpoint_error_m"] <= 1
            (WORK / (name + "-" + algorithm + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            row = {"algorithm": algorithm, "first_seconds": updates[0]["seconds"], "total_seconds": monotonic() - began,
                   "distance_km": result["distance_km"], "objective_cost": result["objective_cost"],
                   "mean_wind": result["analysis"]["mean_wind_speed"], "max_wind": result["analysis"]["max_wind_speed"],
                   "arrival_at": result["arrival_at"], "level": result["level"], "dataset_id": result["dataset_id"],
                   "updates": updates, "diagnostics": result["planning_diagnostics"]}
            report["cases"].append(row)
            (WORK / (name + "-report.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(row, ensure_ascii=False), flush=True)
            break


if __name__ == "__main__":
    main()
