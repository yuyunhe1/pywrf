import os
from math import hypot

os.environ["GFS_DATA_MODE"] = "mock"
os.environ["GFS_NOW_UTC"] = "2026-06-15 08:30 UTC"

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
CYCLE = "2026-06-15 06:00 UTC"


def test_root_redirects_to_frontend():
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://127.0.0.1:5173"


def test_times_and_wind_grid():
    times = client.get("/api/times").json()
    assert "100m AGL" in times["levels"]
    assert "1000m AGL" in times["levels"]
    assert CYCLE in times["forecast_hours_by_cycle"]
    assert times["valid_times"]
    assert times["valid_times"][0]["label"].endswith("北京时间")
    assert any(item["label"] == "2026-06-15 16:00 北京时间" for item in times["valid_times"])
    assert any(item["valid_time"] == "2026-06-15 06:00 UTC" for item in times["valid_times"])
    response = client.get("/api/wind", params={"cycle": CYCLE, "forecast_hour": 3, "level": "100m"})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["velocity"]) == 2
    assert len(payload["velocity"][0]["data"]) == payload["metadata"]["grid"]["nx"] * payload["metadata"]["grid"]["ny"]
    heatmap = client.get("/api/heatmap", params={"cycle": CYCLE, "forecast_hour": 3, "level": "100m"}).json()
    assert len(heatmap["wind_speed"]["data"]) == heatmap["metadata"]["grid"]["nx"] * heatmap["metadata"]["grid"]["ny"]
    assert heatmap["wind_speed"]["max"] >= heatmap["wind_speed"]["min"] >= 0
    rounded_component_speed = hypot(payload["velocity"][0]["data"][0], payload["velocity"][1]["data"][0])
    assert abs(heatmap["wind_speed"]["data"][0] - rounded_component_speed) < 0.002


def test_wind_can_be_queried_by_beijing_valid_time():
    valid_time = "2026-06-15 17:00 北京时间"
    response = client.get("/api/wind", params={"valid_time": valid_time, "level": "100m AGL"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["valid_time_bj"] == valid_time


def test_point_and_route_analysis():
    point = client.get(
        "/api/point",
        params={"lon": 118.66, "lat": 31.12, "cycle": CYCLE, "forecast_hour": 3, "level": "100m AGL"},
    )
    assert point.status_code == 200
    assert point.json()["wind_speed"] > 0

    route = client.post(
        "/api/route/analyze",
        json={
            "points": [[118.6, 31.1], [118.65, 31.12], [118.7, 31.15]],
            "cycle": CYCLE,
            "forecast_hour": 3,
            "level": "100m AGL",
        },
    )
    assert route.status_code == 200
    assert route.json()["samples"]
    assert 0 <= route.json()["danger_ratio"] <= 1
