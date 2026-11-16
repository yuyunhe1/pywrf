"""Public owner/parallel smoke; creates one ephemeral job, never a saved route."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.request import build_opener, ProxyHandler, Request
from uuid import uuid4

import websockets

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / "data/route_refinement_tests"
OPENER = build_opener(ProxyHandler({}))
LOCAL = "http://127.0.0.1:8001"


def api(path, body=None, method=None, base=LOCAL):
    request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"}, method=method)
    with OPENER.open(request, timeout=90) as response:
        return json.load(response)


async def main():
    runtime = json.loads((ROOT / "data/platform_runtime/v3-running.json").read_text(encoding="utf-8-sig"))
    public = runtime["public_url"]
    report = dict(writes_platform_history=False, public_url=public, health=api("/api/health"),
                  frontend_proxy=api("/api/health", base="http://127.0.0.1:5174"),
                  public_health=await asyncio.to_thread(api, "/api/health", base=public))
    body = dict(start=[119.56970, 31.22689], end=[119.96521, 30.55753], source="wrf", level="100m AGL",
                time_aware=True, planner_type="wa_lpa_star", planning_strategy="wind_avoidance",
                average_ground_speed_mps=10, departure_at="2026-11-10T08:37:04.845Z",
                client_instance_id="production-verification-" + uuid4().hex)
    state = await asyncio.to_thread(api, "/api/route/plan/jobs", body, base=public)
    identifier = state["job_id"]
    report.update(job_id=identifier, owner_timeout_seconds=state["owner_timeout_seconds"],
                  timeout_seconds=state["timeout_seconds"], heartbeat_sequences=[])
    path = "/api/route/plan/jobs/" + identifier
    try:
        assert state["owner_timeout_seconds"] == 60 and state["timeout_seconds"] == 7200, state
        uri = public.replace("https://", "wss://").replace("http://", "ws://") + path + "/owner"
        async with websockets.connect(uri, proxy=None, open_timeout=25, max_size=None) as socket:
            await socket.send(json.dumps(dict(client_instance_id=body["client_instance_id"])))
            for _ in range(2):
                heartbeat = json.loads(await asyncio.wait_for(socket.recv(), timeout=20))
                assert heartbeat["type"] == "heartbeat", heartbeat
                await socket.send(json.dumps(dict(type="ack", seq=heartbeat["seq"])))
                report["heartbeat_sequences"].append(heartbeat["seq"])
            state = await asyncio.to_thread(api, path)
            result = state.get("result") or {}
            stats = (state.get("search_progress") or {}).get("statistics", {})
            if not stats:
                stats = (result.get("planning_diagnostics") or {}).get("final_observed_search", {})
            assert state["status"] == "running", state["status"]
            assert result.get("analysis", {}).get("navigation_allowed"), state.get("message")
            assert stats.get("execution_mode") == "parallel_processes", stats
            workers = stats["direction_workers"]
            assert set(workers) == {"forward", "backward"} and workers["forward"]["pid"] != workers["backward"]["pid"]
            assert stats["observed_parallel_overlap_seconds"] > 0, stats
            full_weather = stats["weather"]
            assert full_weather['spatial_risk_model'] == result['spatial_risk_model'] == 'CLOSED_CELL_SUPERCOVER_V2'
            assert result['objective_model'] == 'SUPERCOVER_WIND_EXPOSURE_V3'
            assert result['flight_profile']['spatial_risk_model'] == result['spatial_risk_model']
            assert full_weather["weather_loading_mode"] == "full", full_weather
            assert full_weather["full_field_hours_loaded"] == full_weather["forecast_hour_count"]
            assert all(worker["weather"]["weather_loading_mode"] == "full" and
                       worker["weather"]["full_field_hours_loaded"] == full_weather["forecast_hour_count"]
                       for worker in workers.values())
            report.update(dataset_id=result["dataset_id"], wind_time_step_seconds=result["wind_time_step_seconds"],
                          preparation_seconds=state["preparation_seconds"], queue_wait_seconds=state["queue_wait_seconds"],
                          backend_first_seconds=state["first_feasible_elapsed_seconds"], feasible=True,
                          direction_workers=workers, overlap_seconds=stats["observed_parallel_overlap_seconds"],
                          weather=full_weather, search_domain_bbox=stats["search_domain_bbox"],
                          spatial_risk_model=result['spatial_risk_model'],objective_model=result['objective_model'],
                          search_domain_grid_cells=stats["search_domain_grid_cells"])
            began = time.monotonic()
            await asyncio.to_thread(api, path, method="DELETE")
            while True:
                state = await asyncio.to_thread(api, path)
                if state["compute_finished"]:
                    break
                assert time.monotonic() - began < 2, state
                await asyncio.sleep(.025)
            report.update(cancel_compute_finished=state["compute_finished"], cancel_status=state["status"],
                          cancel_release_seconds=time.monotonic() - began, retained_feasible_result=state["feasible"])
            assert state["status"] == "cancelled" and report["cancel_release_seconds"] < 2
    finally:
        # Only the temporary job made by this script may be cancelled.
        await asyncio.to_thread(api, path, method="DELETE")
        report["verified_at"] = datetime.now(timezone.utc).isoformat()
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "production-readiness.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "direction_workers"}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
