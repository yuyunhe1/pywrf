"""Compare full/local loading on one frozen forecast without writing route history."""
import argparse
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from verify_segmented_planning import pinned_dataset, ROOT
from app.direction_pool import warm_direction_pool, shutdown_direction_pool
from app.models import RoutePlanRequest
from app.planning_control import planning_scope
from app.temporal_plan_service import compute_temporal_plan

WORK = ROOT / "data/full_field_planning_tests"
CASES = {
    "first": ([119.56970, 31.22689], [119.96521, 30.55753], "2026-11-10T08:37:04.845Z", "2026-11-10 00:00 UTC"),
    "anomaly": ([118.75397, 30.39420], [119.13025, 30.08811], "2026-11-10T08:42:18.775Z", "2026-11-10 00:00 UTC"),
    "original": ([117.17468, 30.91165], [118.13049, 30.39657], "2026-11-04T09:00:00Z", "2026-11-04 00:00 UTC"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=CASES, default="first")
    parser.add_argument("--loading", choices=["full", "local"], required=True)
    parser.add_argument("--execution", choices=["serial", "parallel"], default="serial")
    parser.add_argument("--labels", type=int, default=5000)
    args = parser.parse_args()
    os.environ.update(ROUTE_PLAN_WEATHER_LOADING=args.loading, ROUTE_PLAN_SEARCH_FULL_DOMAIN="1",
                      ROUTE_PLAN_DIRECTION_PROCESSES="2" if args.execution == "parallel" else "0",
                      ROUTE_PLAN_MAX_TEMPORAL_LABELS=str(args.labels), ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS="0")
    start, end, departure, cycle = CASES[args.case]
    began = monotonic()
    dataset = pinned_dataset(cycle, "100m AGL")
    preparation = monotonic() - began
    request = RoutePlanRequest(start=start, end=end, source="wrf", level="100m AGL", planner_type="wa_lpa_star",
        planning_strategy="wind_avoidance", departure_at=departure, average_ground_speed_mps=10, time_aware=True)
    if args.execution == "parallel":
        warm_direction_pool()
    publications = []
    began = monotonic()
    def publish(value):
        publications.append(dict(seconds=monotonic()-began, distance_km=value["distance_km"], objective_cost=value["objective_cost"]))
    try:
        with planning_scope(monotonic()+120, Event()):
            result = compute_temporal_plan(request, dataset, [117,29.7,120.15,32.4], 0, publish)
        assert result["analysis"]["navigation_allowed"], result.get("planning_message")
        report = dict(case=args.case, loading=args.loading, execution=args.execution, label_budget=args.labels,
                      writes_platform_history=False, preparation_seconds=preparation, seconds=monotonic()-began,
                      publications=publications, dataset_id=dataset.dataset_id, source_hashes=dataset.source_hashes,
                      diagnostics=result["planning_diagnostics"], points=result["points"],
                      distance_km=result["distance_km"], objective_cost=result["objective_cost"])
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK/f"{args.case}-{args.execution}-{args.loading}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        d = report["diagnostics"]
        a = d["attempts"][0]
        print(json.dumps(dict(case=args.case,loading=args.loading,execution=args.execution,first=publications[0]["seconds"],
              total=report["seconds"],km=result["distance_km"],cost=result["objective_cost"],
              field=d["weather"],domain=a["search_domain_bbox"],grid_cells=a["search_domain_grid_cells"],
              expanded=a["expanded_nodes"],forward_bbox=a["forward_expanded_bbox"],backward_bbox=a["backward_expanded_bbox"]),ensure_ascii=False))
    finally:
        shutdown_direction_pool()


if __name__ == "__main__":
    main()
