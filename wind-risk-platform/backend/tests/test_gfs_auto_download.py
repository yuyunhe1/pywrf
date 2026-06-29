from fastapi.testclient import TestClient


def _empty_availability():
    return {
        "cycles": [],
        "forecast_hours": [],
        "forecast_hours_by_cycle": {},
        "valid_times": [],
        "levels": ["100m AGL"],
    }


def _running_status():
    return {
        "enabled": True,
        "running": True,
        "message": "download running",
        "log_path": "download.log",
    }


def test_times_starts_realtime_download_when_gfs_is_empty(monkeypatch):
    from app import gfs_downloader, gfs_provider
    from app.main import app

    calls = []
    monkeypatch.setattr(gfs_provider, "availability", _empty_availability)
    monkeypatch.setattr(gfs_downloader, "status", lambda: {"enabled": True, "running": False})
    monkeypatch.setattr(
        gfs_downloader,
        "start_realtime_download",
        lambda reason, force=False: calls.append(reason) or _running_status(),
    )

    client = TestClient(app)
    response = client.get("/api/times", params={"source": "gfs"})

    assert response.status_code == 200
    assert calls == ["no selectable realtime GFS files"]
    assert response.json()["download"]["running"] is True


def test_missing_gfs_grid_starts_download_and_returns_503(monkeypatch):
    from app import gfs_downloader, gfs_provider
    from app.main import app

    calls = []
    monkeypatch.setattr(gfs_provider, "find_file", lambda cycle, forecast_hour: (_ for _ in ()).throw(ValueError("no GFS GRIB2 file")))
    monkeypatch.setattr(
        gfs_downloader,
        "start_realtime_download",
        lambda reason, force=False: calls.append(reason) or _running_status(),
    )

    client = TestClient(app)
    response = client.get(
        "/api/wind",
        params={
            "source": "gfs",
            "cycle": "2026-06-15 06:00 UTC",
            "forecast_hour": 3,
            "level": "100m AGL",
        },
    )

    assert response.status_code == 503
    assert calls == ["no GFS GRIB2 file"]
    assert response.json()["detail"]["download"]["running"] is True
