from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import departure_windows as windows
from app import wrf_cache_provider as wrf
from app.route_service import haversine_km
from app.wind_provider import WindGrid

BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)
SHORT = [(0.01, 0.0), (0.02, 0.0)]
LONG = [(0.01, 0.0), (0.39, 0.0)]


def frame(hour, u=2.0, v=0.0):
    valid = BASE + timedelta(hours=hour)
    return valid.timestamp(), WindGrid(
        lons=np.array([0.0, 0.1, 0.2, 0.3, 0.4]), lats=np.array([-0.1, 0, 0.1]),
        u=np.broadcast_to(np.asarray(u, dtype=float), (3, 5)).copy(),
        v=np.broadcast_to(np.asarray(v, dtype=float), (3, 5)).copy(),
        cycle="2026-11-01 00:00 UTC", forecast_hour=hour, level="100m AGL",
        valid_time=valid.strftime("%Y-%m-%d %H:%M UTC"),
    )


def evaluate(frames, points=SHORT, minutes=10, speed=10, shear=None):
    prepared = windows.prepare_evaluation(points, frames, speed, 7.9, shear)
    return windows.describe_evaluation(prepared, BASE + timedelta(minutes=minutes), speed)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def test_safe_windows_account_for_entire_flight_and_round_inwards():
    frames = [frame(hour) for hour in range(5)]
    result = evaluate(frames)
    assert result["current_departure"]["status"] == "allowed"
    assert result["flight_duration_seconds"] == pytest.approx(haversine_km(*SHORT) * 100)
    assert result["departure_windows"] == [{
        "start": "2026-11-01T00:10:00.000Z", "end": "2026-11-01T03:55:00.000Z",
        "first_arrival": result["departure_windows"][0]["first_arrival"],
        "last_arrival": result["departure_windows"][0]["last_arrival"],
    }]
    assert timestamp(result["departure_windows"][-1]["last_arrival"]) < (BASE + timedelta(hours=4)).timestamp()
    duration = windows.prepare_evaluation(SHORT, frames, 10, 7.9, None).duration_s
    ending_exactly_at_forecast_end = evaluate(frames, minutes=(4 * 3600 - duration) / 60)
    assert ending_exactly_at_forecast_end["current_departure"]["status"] == "allowed"
    assert evaluate(frames, minutes=240)["current_departure"]["status"] == "unknown"


def test_departure_safe_now_can_be_blocked_after_crossing_hour():
    frames = [frame(0, 1), frame(1, 10), frame(2, 1), frame(3, 1), frame(4, 1)]
    result = evaluate(frames, points=LONG, minutes=30)
    assert 1 + 9 * 0.5 < 7.9  # Entire route is safe on the departure snapshot.
    assert result["current_departure"]["status"] == "blocked"
    assert result["current_departure"]["first_issue"]["type"] == "wind"
    assert result["departure_windows"][0]["start"] == "2026-11-01T01:20:00.000Z"
    assert evaluate(frames, points=LONG, minutes=30, speed=50)["current_departure"]["status"] == "allowed"


def test_weather_at_far_position_before_aircraft_arrives_is_not_a_block():
    frames = [frame(0, [2, 2, 2, 2, 10]), *[frame(hour, 2) for hour in range(1, 5)]]
    assert evaluate(frames, points=LONG, minutes=0)["current_departure"]["status"] == "allowed"


def test_weather_becoming_dangerous_after_passing_a_cell_does_not_block_departure():
    frames = [frame(0, 1), *[frame(hour, [10, 1, 1, 1, 1]) for hour in range(1, 5)]]
    # The first cell is left within eight minutes. It only becomes unsafe
    # around minute 46, while the aircraft is already in later safe cells.
    assert evaluate(frames, points=LONG, minutes=0, shear=5.4)["current_departure"]["status"] == "allowed"


def test_hourly_interpolation_uses_vectors_instead_of_scalar_speed():
    frames = [frame(0, 8), frame(1, -8), frame(2, 8)]
    result = evaluate(frames, minutes=30)
    assert result["current_departure"]["status"] == "allowed"
    assert len(result["departure_windows"]) == 2
    assert result["departure_windows"][0]["end"] == "2026-11-01T00:55:00.000Z"
    assert result["departure_windows"][1]["start"] == "2026-11-01T01:10:00.000Z"


def test_horizontal_shear_is_evaluated_spatially_at_the_same_arrival_time():
    spatial = [frame(hour, [-4, -4, -4, 4, 4]) for hour in range(4)]
    blocked = evaluate(spatial, points=LONG, minutes=0, shear=5.4)
    assert blocked["current_departure"]["status"] == "blocked"
    assert blocked["current_departure"]["first_issue"]["type"] == "shear"
    assert evaluate(spatial, points=LONG, minutes=0, shear=None)["current_departure"]["status"] == "allowed"
    # A spatially uniform wind changing with time creates no horizontal shear.
    temporal = [frame(0, -4), frame(1, 4), frame(2, -4), frame(3, 4)]
    assert evaluate(temporal, points=LONG, minutes=0, shear=0.1)["current_departure"]["status"] == "allowed"


def test_equal_threshold_is_forbidden_without_rounding_safe_values_up():
    assert evaluate([frame(hour, 7.9) for hour in range(3)])["current_departure"]["status"] == "blocked"
    assert evaluate([frame(hour, 7.8999) for hour in range(3)])["current_departure"]["status"] == "allowed"
    equal_shear = [frame(hour, [0, 0, 0, 5.4, 5.4]) for hour in range(4)]
    assert evaluate(equal_shear, points=LONG, shear=5.4)["current_departure"]["status"] == "blocked"


def test_missing_hour_and_nonfinite_cells_are_unknown_but_do_not_hide_later_windows():
    result = evaluate([frame(0), frame(2), frame(3)])
    assert result["current_departure"]["status"] == "unknown"
    assert result["coverage_incomplete"] is True
    assert result["departure_windows"][0]["start"] == "2026-11-01T02:00:00.000Z"
    assert evaluate([frame(0), frame(1, [np.nan, 2, 2, 2, 2]), frame(2)])["current_departure"]["status"] == "unknown"
    assert evaluate([frame(hour, [2, 2, 2, 2, np.nan]) for hour in range(3)])["current_departure"]["status"] == "allowed"


def test_outside_domain_or_stale_forecast_cannot_be_allowed():
    outside = evaluate([frame(hour) for hour in range(3)], points=[(0.01, 0), (0.5, 0)])
    assert outside["current_departure"]["status"] == "unknown"
    assert "空间" in outside["current_departure"]["reason"]
    assert outside["departure_windows"] == []
    stale = evaluate([frame(0), frame(1)], minutes=200)
    assert stale["current_departure"]["status"] == "unknown"
    assert stale["empty_reason"] == "data_insufficient"
    assert evaluate([])["current_departure"]["status"] == "unknown"
    with pytest.raises(ValueError):
        evaluate([frame(0), frame(1)], points=[(0, 0), (0, 0)])


def test_safe_departures_are_not_merged_across_a_ten_minute_hazard():
    points = [(0.39, 0), (0.05, 0)]
    speed = haversine_km(*points) * 1000 / (58 * 60)
    frames = [frame(0, [6.99, 0, 0, 0, 0]), frame(1, [7.91, 0, 0, 0, 0]), frame(2, [6.99, 0, 0, 0, 0])]
    result = evaluate(frames, points=points, minutes=0, speed=speed)
    assert result["current_departure"]["status"] == "allowed"
    assert evaluate(frames, points=points, minutes=2, speed=speed)["current_departure"]["status"] == "blocked"
    assert evaluate(frames, points=points, minutes=5, speed=speed)["current_departure"]["status"] == "blocked"
    assert evaluate(frames, points=points, minutes=15, speed=speed)["current_departure"]["status"] == "allowed"
    assert len(result["departure_windows"]) == 2
    assert result["departure_windows"][0]["end"] == "2026-11-01T00:00:00.000Z"
    assert result["departure_windows"][1]["start"] == "2026-11-01T00:15:00.000Z"


def write_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("WRF_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WRF_CACHE_AUTO_SYNC_INDEX", "0")
    records = []
    for hour in range(4):
        _, grid = frame(hour)
        relative = f"cycle/hour-{hour}.npz"
        file = tmp_path / relative
        file.parent.mkdir(exist_ok=True)
        np.savez_compressed(file, lons=grid.lons, lats=grid.lats, levels_m=np.array([100]),
                            u=grid.u[None], v=grid.v[None], cycle_utc=np.array(grid.cycle),
                            valid_time_utc=np.array(grid.valid_time), forecast_hour=np.array(hour))
        records.append({"cycle": grid.cycle, "forecast_hour": hour, "valid_time": grid.valid_time, "path": relative})
    (tmp_path / "index.json").write_text(json.dumps({"files": records, "levels": ["100m AGL"]}), encoding="utf-8")
    wrf.load_index.cache_clear()
    wrf._get_grid_cached.cache_clear()
    windows._CACHE.clear()
    return records


def test_endpoint_uses_saved_route_and_invalidates_cache_when_products_change(tmp_path, monkeypatch):
    from app import main
    records = write_cache(tmp_path, monkeypatch)
    monkeypatch.setenv("GFS_NOW_UTC", "2026-11-01 00:10 UTC")
    route = {"route_id": "fixed", "name": "固定航线", "level": "100m AGL", "points": SHORT}
    monkeypatch.setattr(main.route_storage, "get_route", lambda route_id: route if route_id == "fixed" else None)
    client = TestClient(main.app)
    result = client.post("/api/routes/fixed/departure-windows", json={})
    assert result.status_code == 200
    assert result.json()["level"] == "100m AGL"
    assert result.json()["current_departure"]["status"] == "allowed"
    assert client.post("/api/routes/absent/departure-windows", json={}).status_code == 404
    for speed in [0, -1, "Infinity", "NaN"]:
        assert client.post("/api/routes/fixed/departure-windows", json={"average_ground_speed_mps": speed}).status_code == 422
    assert client.post("/api/routes/fixed/departure-windows", json={"average_ground_speed_mps": 1e-309}).status_code == 400
    # Atomic replacement changes the revision and must invalidate both caches.
    file = tmp_path / records[1]["path"]
    with np.load(file) as data:
        changed = {key: data[key] for key in data.files}
    changed["u"] = np.full_like(changed["u"], np.nan)
    np.savez_compressed(file, **changed)
    assert client.post("/api/routes/fixed/departure-windows", json={}).json()["current_departure"]["status"] == "unknown"


def test_cycle_selection_never_stitches_two_cycles(tmp_path, monkeypatch):
    records = write_cache(tmp_path, monkeypatch)
    newer = {**records[-1], "cycle": "2026-11-01 01:00 UTC", "path": "missing-new-cycle.npz"}
    (tmp_path / "index.json").write_text(json.dumps({"files": [*records, newer]}))
    wrf.load_index.cache_clear()
    cycle, selected = wrf.local_forecast_records(BASE + timedelta(minutes=10))
    assert cycle == "2026-11-01 00:00 UTC"
    assert selected == records
    assert all(record["cycle"] == cycle for record in selected)


def test_future_only_local_coverage_preserves_future_windows_without_extrapolating_now(tmp_path, monkeypatch):
    from app.models import DepartureWindowsRequest
    records = write_cache(tmp_path, monkeypatch)
    (tmp_path / records[0]["path"]).unlink()
    now = BASE + timedelta(minutes=10)
    cycle, selected = wrf.local_forecast_records(now)
    assert cycle == "2026-11-01 00:00 UTC"
    assert selected == records
    result = windows.evaluate_saved_route(
        {"route_id": "future-only", "name": "未来可用", "points": SHORT, "level": "100m AGL"},
        DepartureWindowsRequest(), now,
    )
    assert result["current_departure"]["status"] == "unknown"
    assert result["coverage_incomplete"] is True
    assert result["departure_windows"][0]["start"] == "2026-11-01T01:00:00.000Z"
