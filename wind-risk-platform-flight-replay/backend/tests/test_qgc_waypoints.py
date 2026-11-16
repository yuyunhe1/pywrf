from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import RouteRecord
from app.qgc_waypoints import (
    QGC_WPL_HEADER,
    build_qgc_mission_items,
    parse_qgc_waypoints,
    route_points_from_qgc_items,
    serialize_qgc_waypoints,
)


SAMPLE_FILE = Path(__file__).resolve().parents[3] / "yifulou.waypoints"
client = TestClient(main.app)


def test_real_mission_planner_file_parses_all_commands():
    items = parse_qgc_waypoints(SAMPLE_FILE.read_text(encoding="utf-8"))

    assert len(items) == 19
    assert items[0]["command"] == 16
    assert items[0]["current"] == 1
    assert items[1]["command"] == 22
    assert items[1]["param1"] == pytest.approx(15.0)
    assert items[7]["command"] == 177
    assert items[-1]["command"] == 21
    assert items[-1]["latitude"] == pytest.approx(31.7724087)
    assert items[-1]["longitude"] == pytest.approx(117.1928215)


def test_real_waypoints_round_trip_without_losing_task_fields():
    original = parse_qgc_waypoints(SAMPLE_FILE.read_text(encoding="utf-8"))
    serialized = serialize_qgc_waypoints(original)
    restored = parse_qgc_waypoints(serialized)

    assert serialized.startswith(f"{QGC_WPL_HEADER}\r\n")
    assert restored == original


def test_map_points_skip_commands_without_coordinates():
    items = parse_qgc_waypoints(SAMPLE_FILE.read_text(encoding="utf-8"))
    points = route_points_from_qgc_items(items)

    assert len(points) == 17
    assert points[0]["lat"] == pytest.approx(31.7722348)
    assert points[0]["lon"] == pytest.approx(117.1925207)
    assert points[-1]["lat"] == pytest.approx(31.7724087)
    assert points[-1]["lon"] == pytest.approx(117.1928215)


def test_platform_route_generates_home_takeoff_waypoint_and_land():
    items = build_qgc_mission_items(
        [
            [117.19, 31.77, 40.0, 30.0, 10.0],
            [117.20, 31.78, 42.0, 30.0, 12.0],
            [117.21, 31.79, 45.0, 25.0, 20.0],
        ],
        default_agl_m=30.0,
    )

    assert [item["command"] for item in items] == [16, 22, 16, 21]
    assert items[0]["frame"] == 0
    assert items[0]["altitude"] == pytest.approx(40.0)
    assert items[1]["altitude"] == pytest.approx(30.0)
    assert items[2]["latitude"] == pytest.approx(31.78)
    assert items[2]["longitude"] == pytest.approx(117.20)
    assert items[-1]["latitude"] == pytest.approx(31.79)
    assert items[-1]["longitude"] == pytest.approx(117.21)
    assert items[-1]["altitude"] == 0.0


@pytest.mark.parametrize(
    "text, message",
    [
        ("QGC WPL 120\n", "首行必须是 QGC WPL 110"),
        ("QGC WPL 110\n0 1 0 16", "应有 12 列"),
    ],
)
def test_invalid_waypoint_files_are_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        parse_qgc_waypoints(text)


def test_waypoint_parse_endpoint_returns_map_points_and_raw_mission_items():
    response = client.post(
        "/api/waypoints/parse",
        content=SAMPLE_FILE.read_bytes(),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["format"] == QGC_WPL_HEADER
    assert len(payload["mission_items"]) == 19
    assert len(payload["points"]) == 17
    assert payload["mission_items"][7]["command"] == 177


def test_export_creates_matching_json_and_waypoint_files(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "EXPORT_DIR", tmp_path)
    record = RouteRecord(
        name="测试航线",
        start=(117.19, 31.77),
        end=(117.21, 31.79),
        points=[
            (117.19, 31.77, 40.0, 30.0, 10.0),
            (117.20, 31.78, 42.0, 30.0, 12.0),
            (117.21, 31.79, 45.0, 25.0, 20.0),
        ],
        level="30m AGL",
    )

    exported = main.export_route_to_json(record, "route-test")
    json_path = tmp_path / exported["file_name"]
    waypoint_path = tmp_path / exported["waypoint_file_name"]

    assert json_path.exists()
    assert waypoint_path.exists()
    assert json_path.stem == waypoint_path.stem
    assert [item["command"] for item in parse_qgc_waypoints(waypoint_path.read_text())] == [16, 22, 16, 21]


def test_imported_mission_commands_survive_json_waypoint_export(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "EXPORT_DIR", tmp_path)
    mission_items = parse_qgc_waypoints(SAMPLE_FILE.read_text(encoding="utf-8"))
    points = route_points_from_qgc_items(mission_items)
    record = RouteRecord(
        name="逸夫楼",
        start=(points[0]["lon"], points[0]["lat"]),
        end=(points[-1]["lon"], points[-1]["lat"]),
        points=[
            (
                point["lon"],
                point["lat"],
                point.get("altitude_amsl_m", 0.0),
                point.get("altitude_agl_m", 0.0),
                point.get("terrain_height_m", 0.0),
            )
            for point in points
        ],
        level="30m AGL",
        mission_items=mission_items,
    )

    exported = main.export_route_to_json(record, "route-real")
    restored = parse_qgc_waypoints((tmp_path / exported["waypoint_file_name"]).read_text())

    assert restored == mission_items


def test_exported_file_rename_and_delete_keep_pair_in_sync(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "EXPORT_DIR", tmp_path)
    monkeypatch.setattr(main, "_unique_route_id_by_name", lambda _name: None)
    json_path = tmp_path / "old.json"
    waypoint_path = tmp_path / "old.waypoints"
    json_path.write_text('{"mission_name": "仅用于文件测试"}', encoding="utf-8")
    waypoint_path.write_text(SAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8")

    renamed = client.put(
        "/api/exported-routes/old.json/rename",
        json={"file_name": "new.json"},
    )

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["waypoint_file_name"] == "new.waypoints"
    assert not json_path.exists()
    assert not waypoint_path.exists()
    assert (tmp_path / "new.json").exists()
    assert (tmp_path / "new.waypoints").exists()

    waypoint_download = client.get("/api/exported-waypoints/new.waypoints")
    assert waypoint_download.status_code == 200
    assert waypoint_download.text.startswith(QGC_WPL_HEADER)

    deleted = client.delete("/api/exported-routes/new.json")
    assert deleted.status_code == 204
    assert not (tmp_path / "new.json").exists()
    assert not (tmp_path / "new.waypoints").exists()
