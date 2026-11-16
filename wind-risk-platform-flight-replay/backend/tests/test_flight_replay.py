from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main, wrf_cache_provider as wrf, wrf_timeline as timeline
from app.flight_replay import build_profile
from app.models import FlightProfileRequest
from app.wind_provider import WindGrid

BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)
POINTS = [(0.01, 0), (0.39, 0)]
ROUTE = {"route_id": "timed", "name": "跨小时航线", "level": "100m AGL", "points": POINTS}


def dataset(values, v=None):
    frames = []
    for hour, value in values:
        grid = WindGrid(np.array([0., .1, .2, .3, .4]), np.array([.1, 0, -.1]),
                        np.broadcast_to(np.array(value, dtype=float), (3, 5)).copy(),
                        np.broadcast_to(np.array(0 if v is None else v, dtype=float), (3, 5)).copy(),
                        "2026-11-01 00:00 UTC", hour, "100m AGL",
                        (BASE + timedelta(hours=hour)).strftime("%Y-%m-%d %H:%M UTC"))
        frames.append(((BASE + timedelta(hours=hour)).timestamp(), grid))
    return timeline.ForecastDataset("test", frames[0][1].cycle if frames else None, "100m AGL", tuple(frames),
                                    frames[0][0] if frames else None, frames[-1][0] if frames else None)


def profile(data, minutes=0, speed=10, route=ROUTE):
    return build_profile(route, FlightProfileRequest(departure_at=BASE + timedelta(minutes=minutes),
                                                     average_ground_speed_mps=speed), data)


def test_current_map_can_be_windy_at_destination_but_timed_route_is_safe():
    data = dataset([(0, [2, 2, 2, 2, 10]), *[(h, 2) for h in range(1, 5)]])
    result = profile(data)
    assert result["decision"]["status"] == "allowed"
    assert result["analysis"]["samples"][-1]["wind_speed"] == pytest.approx(2)
    assert timeline.vector_at(data, BASE.timestamp(), .39, 0)[0] == 10
    assert not any(s["status"] == "blocked" for s in result["segments"])
    assert len(result["frame_times"]) == 5


@pytest.mark.parametrize('source',['wrf','gfs'])
def test_unpinned_profile_recheck_respects_the_selected_cycle(monkeypatch,source):
    from dataclasses import replace
    from app import flight_replay as replay
    data=replace(dataset([(h,1) for h in range(6)]),source=source)
    seen=[]
    def selected(level,now,cycle=None):
        seen.append(cycle)
        return data
    monkeypatch.setattr(replay,'load_gfs_dataset' if source=='gfs' else 'load_dataset',selected)
    req=FlightProfileRequest(departure_at=BASE,cycle='2026-11-01 00:00 UTC',source=source)
    result=replay.evaluate_flight({**ROUTE,'source':source},req)
    assert seen==['2026-11-01 00:00 UTC'] and result['decision']['status']=='allowed'


def test_whole_flight_decision_detects_danger_after_departure_and_speed_changes_eta():
    data = dataset([(0, 1), (1, 10), (2, 1), (3, 1)])
    slow, fast = profile(data, minutes=30), profile(data, minutes=30, speed=50)
    assert slow["decision"]["status"] == "blocked"
    assert fast["decision"]["status"] == "allowed"
    assert slow["flight_duration_seconds"] == pytest.approx(5 * fast["flight_duration_seconds"])
    assert any(segment["status"] == "blocked" for segment in slow["segments"])
    for sample in slow["analysis"]["samples"][::30]:
        stamp = datetime.fromisoformat(sample["eta"].replace("Z", "+00:00")).timestamp()
        assert stamp == pytest.approx(BASE.timestamp() + 1800 + sample["distance_km"] * 100, abs=.001)
        vector = timeline.vector_at(data, stamp, sample["lon"], sample["lat"])
        assert sample["wind_speed"] == pytest.approx(np.hypot(*vector), abs=.00001)


def test_reversal_interpolates_uv_and_does_not_create_false_horizontal_shear():
    data = dataset([(0, 8), (1, -8), (2, 8)])
    result = profile(data, minutes=30, speed=100)
    assert result["analysis"]["samples"][0]["wind_speed"] == 0
    assert result["analysis"]["wind_shear"]["max_horizontal_wind_shear"] == 0
    assert not result["events"]
    frame = timeline.wind_frame(data, BASE.timestamp() + 1800)
    assert all(value == 0 for value in frame["velocity"][0]["data"])


def test_spatial_shear_is_checked_at_the_crossing_time_and_has_a_map_event():
    data = dataset([(h, [-4, -4, -4, 4, 4]) for h in range(4)])
    result = profile(data)
    assert result["decision"]["status"] == "blocked"
    assert result["events"]
    assert result["analysis"]["wind_shear"]["max_horizontal_wind_shear"] == 8
    assert any(item["hard_constraint_triggered"] for item in result["analysis"]["wind_shear"]["horizontal_wind_shear_profile"])


@pytest.mark.parametrize("data", [dataset([(0, 2), (2, 2), (3, 2)]), dataset([(0, 2), (1, 2)]), dataset([])])
def test_missing_hours_or_forecast_end_produce_grey_segments_and_nulls(data):
    result = profile(data, minutes=30)
    assert result["decision"]["status"] == "unknown"
    assert any(s["status"] == "unknown" and s["color"] == "#94a3b8" for s in result["segments"])
    assert any(s["wind_speed"] is None for s in result["analysis"]["samples"])
    assert result["analysis"]["coverage_incomplete"] is True
    json.dumps(result, allow_nan=False)


def test_nonfinite_cell_is_missing_in_both_map_and_route():
    data = dataset([(h, [np.nan, 2, 2, 2, 2]) for h in range(4)])
    result = profile(data)
    assert result["decision"]["status"] == "unknown"
    assert result["analysis"]["samples"][0]["wind_speed"] is None
    frame = timeline.wind_frame(data, BASE.timestamp() + 1800)
    assert frame["velocity"][0]["data"][0] is None
    assert frame["velocity"][1]["data"][0] is None
    json.dumps(frame, allow_nan=False)


def test_route_geometry_preserves_turns_and_outside_portion_is_grey():
    points = [(0.01, 0), (.02, .02), (.03, 0), (.5, 0)]
    data = dataset([(h, 2) for h in range(5)])
    result = profile(data, route={**ROUTE, "points": points})
    assert result["decision"]["status"] == "unknown"
    line_points = [p for segment in result["segments"] for p in segment["points"]]
    assert list(points[1]) in line_points and list(points[2]) in line_points
    assert result["segments"][-1]["status"] == "unknown"
    assert result["segments"][0]["status"] == "allowed"


def test_short_danger_interval_remains_visible_in_profile_segments():
    data = dataset([(0, 7.0), (1, 7.90001), (2, 7.0), (3, 7.0)])
    result = profile(data, minutes=55, speed=10)
    assert result["decision"]["status"] == "blocked"
    assert any(s["status"] == "blocked" for s in result["segments"])


def write_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("WRF_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WRF_CACHE_AUTO_SYNC_INDEX", "0")
    monkeypatch.setenv("GFS_NOW_UTC", "2026-11-01 00:00 UTC")
    source = dataset([(h, 2) for h in range(5)])
    records = []
    for _, grid in source.frames:
        file = tmp_path / f"f{grid.forecast_hour}.npz"
        np.savez_compressed(file, lons=grid.lons, lats=grid.lats, levels_m=np.array([100]),
                            u=grid.u[None], v=grid.v[None], cycle_utc=np.array(grid.cycle),
                            valid_time_utc=np.array(grid.valid_time), forecast_hour=np.array(grid.forecast_hour))
        records.append({"cycle": grid.cycle, "valid_time": grid.valid_time, "forecast_hour": grid.forecast_hour, "path": file.name})
    (tmp_path / "index.json").write_text(json.dumps({"files": records, "levels": ["100m AGL"]}))
    wrf.load_index.cache_clear()
    wrf._get_grid_cached.cache_clear()
    monkeypatch.setattr(main.route_storage, "get_route", lambda route_id: ROUTE if route_id == "timed" else None)
    return TestClient(main.app)


def test_endpoints_share_dataset_and_old_revision_is_frozen(tmp_path, monkeypatch):
    client = write_dataset(tmp_path, monkeypatch)
    windows = client.post("/api/routes/timed/departure-windows", json={}).json()
    identifier = windows["dataset_id"]
    payload = {"dataset_id": identifier, "departure_at": "2026-11-01T00:00:00Z"}
    result = client.post("/api/routes/timed/flight-profile", json=payload)
    assert result.status_code == 200
    assert result.json()["decision"]["status"] == windows["current_departure"]["status"]
    at = "2026-11-01T00:30:00Z"
    before = client.get("/api/flight-replay/wind", params={"dataset_id": identifier, "at": at}).json()
    file = tmp_path / "f1.npz"
    with np.load(file) as old:
        changed = {key: old[key] for key in old.files}
    changed["u"] = np.full_like(changed["u"], 12)
    np.savez_compressed(file, **changed)
    updated = client.post("/api/routes/timed/departure-windows", json={}).json()
    assert updated["dataset_id"] != identifier
    after = client.get("/api/flight-replay/wind", params={"dataset_id": identifier, "at": at}).json()
    assert before == after
    fresh = client.get("/api/flight-replay/wind", params={"dataset_id": updated["dataset_id"], "at": at}).json()
    assert fresh["velocity"][0]["data"][0] == 7


def test_api_rejects_unzoned_time_expired_dataset_and_wrong_height(tmp_path, monkeypatch):
    client = write_dataset(tmp_path, monkeypatch)
    identifier = client.post("/api/routes/timed/departure-windows", json={}).json()["dataset_id"]
    for at in ["2026-11-01T00:00:00", "not-a-date"]:
        assert client.post("/api/routes/timed/flight-profile", json={"departure_at": at}).status_code == 422
    assert client.post("/api/routes/absent/flight-profile", json={"departure_at": BASE.isoformat()}).status_code == 404
    assert client.post("/api/routes/timed/flight-profile", json={"departure_at": BASE.isoformat(), "dataset_id": "expired"}).status_code == 409
    assert client.get("/api/flight-replay/wind", params={"dataset_id": identifier, "at": "2026-11-01T00:00:00"}).status_code == 422
    assert client.get("/api/flight-replay/wind", params={"dataset_id": identifier, "at": "2026-11-01T05:00:00Z"}).json()["available"] is False
    wrong = timeline.ForecastDataset("wrong-height", None, "300m AGL", (), None, None)
    monkeypatch.setitem(timeline._DATASETS, "wrong-height", wrong)
    assert client.post("/api/routes/timed/flight-profile", json={"departure_at": BASE.isoformat(), "dataset_id": "wrong-height"}).status_code == 400
