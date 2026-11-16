"""Read-only live smoke: creates an ephemeral planning job, never a saved route."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler, Request

from verify_segmented_planning import WORK

OPENER = build_opener(ProxyHandler({}))
BASE = "http://127.0.0.1:8001"


def api(path, body=None, method=None, base=BASE):
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json"}, method=method)
    with OPENER.open(req, timeout=90) as response:
        return json.load(response)


def main():
    report = dict(health=api("/api/health"), frontend_proxy=api("/api/health", base="http://127.0.0.1:5174"),
                  writes_platform_history=False)
    options = api("/api/times?source=wrf")
    cycle = max(options["cycles"])
    rows = [row for row in options["valid_times"] if row["cycle"] == cycle]
    now = datetime.now(timezone.utc)
    row = min(rows, key=lambda r: abs((datetime.strptime(r["valid_time"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc) - now).total_seconds()))
    field = api("/api/wind?" + urlencode(dict(source="wrf", valid_time=row["valid_time"], level="100m AGL", bbox="73,18,135,54")))
    u, v = field["velocity"]
    header = u["header"]
    nx, ny = header["nx"], header["ny"]
    choices = [(math.hypot(u["data"][r * nx + c], v["data"][r * nx + c]), r, c)
               for r in range(2, ny - 2) for c in range(2, nx - 2)
               if u["data"][r * nx + c] is not None and v["data"][r * nx + c] is not None]
    speed, r, c = min(choices)
    assert speed < 7.9
    lon = header["lo1"] + c * header["dx"]
    lat = header["la1"] - r * header["dy"]
    departure = datetime.strptime(row["valid_time"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).isoformat()
    payload = dict(source="wrf", level="100m AGL", planner_type="wa_lpa_star", time_aware=True,
                   departure_at=departure, start=[lon, lat], end=[lon + header["dx"] / 20, lat], average_ground_speed_mps=10)
    state = api("/api/route/plan/jobs", payload)
    report["job_id"], report["timeout_seconds"] = state["job_id"], state["timeout_seconds"]
    began = time.monotonic()
    latest = None
    while time.monotonic() - began < 30:
        state = api("/api/route/plan/jobs/" + state["job_id"])
        if state.get("result"):
            latest = state["result"]
        if latest or state["status"] in {"completed", "failed", "timed_out", "cancelled"}:
            break
        time.sleep(.1)
    if state["status"] not in {"completed", "failed", "timed_out", "cancelled"}:
        state = api("/api/route/plan/jobs/" + state["job_id"], method="DELETE")
        latest = state.get("result") or latest
    assert latest and latest["analysis"]["navigation_allowed"], state
    assert latest["temporal_search_model"] == "TEN_MINUTE_BIDIRECTIONAL_INTERVAL_V1"
    assert state["timeout_seconds"] == 7200
    assert state["progress_revision"] > 0 and state["search_progress"] is None
    assert latest["wind_time_step_seconds"] == 600 and latest["source"] == "wrf"
    report.update(status=state["status"], progress_revision=state["progress_revision"], search_progress=state["search_progress"],
                  search_model=latest["temporal_search_model"], dataset_id=latest["dataset_id"], feasible=True,
                  backend_first_seconds=latest["planning_diagnostics"]["first_feasible_seconds"],
                  wind_time_step_seconds=latest["wind_time_step_seconds"], verified_at=datetime.now(timezone.utc).isoformat())
    (WORK / "production-readiness.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
