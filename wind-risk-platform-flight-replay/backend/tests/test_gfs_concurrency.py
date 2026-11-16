from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app


def test_wind_and_heatmap_can_load_concurrently(monkeypatch):
    """The frontend requests both endpoints in parallel on every wind-field load."""
    monkeypatch.setenv("GFS_DATA_MODE", "mock")
    client = TestClient(app)
    params = {
        "cycle": "2026-06-15 06:00 UTC",
        "forecast_hour": 3,
        "level": "100m AGL",
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda endpoint: client.get(endpoint, params=params), ["/api/wind", "/api/heatmap"]))

    assert [response.status_code for response in responses] == [200, 200]
