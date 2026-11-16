from fastapi.testclient import TestClient
from io import StringIO


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


def test_connection_refused_detection_supports_windows_error():
    from app import gfs_downloader

    assert gfs_downloader._is_connection_refused(
        "<urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>"
    )


def test_downloader_output_is_mirrored_and_failed_count_is_parsed(capsys):
    from app import gfs_downloader

    class FakeProcess:
        stdout = iter(
            [
                "[ERROR] f012 attempt 3/3: <urlopen error [WinError 10061]>\n",
                "[SUMMARY] total=12, downloaded=0, fallback=0, skipped=0, failed=12\n",
            ]
        )

    log_file = StringIO()
    connection_refused, failed_files = gfs_downloader._stream_process_output(
        FakeProcess(), log_file
    )

    terminal = capsys.readouterr()
    assert connection_refused is True
    assert failed_files == 12
    assert "[ERROR] f012 attempt 3/3" in terminal.out
    assert "[GFS 网络提示]" in terminal.err
    assert "[GFS 网络提示]" in log_file.getvalue()
