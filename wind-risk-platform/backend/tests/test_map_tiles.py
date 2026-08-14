import pytest

from app import map_tiles


PNG_DATA = b"\x89PNG\r\n\x1a\n" + b"test-png-data"


def test_invalid_tile_coordinates_are_rejected():
    with pytest.raises(map_tiles.TileCoordinateError):
        map_tiles.validate_tile_coordinate(3, 8, 0)
    with pytest.raises(map_tiles.TileCoordinateError):
        map_tiles.validate_tile_coordinate(-1, 0, 0)


def test_tile_is_downloaded_once_then_read_from_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(map_tiles, "TILE_CACHE_DIR", tmp_path)
    calls = []

    def fake_download(z, x, y):
        calls.append((z, x, y))
        return PNG_DATA

    monkeypatch.setattr(map_tiles, "_download_tile", fake_download)

    first_path, first_downloaded = map_tiles.get_cached_tile(3, 6, 3)
    second_path, second_downloaded = map_tiles.get_cached_tile(3, 6, 3)

    assert first_path == second_path
    assert first_path.read_bytes() == PNG_DATA
    assert first_downloaded is True
    assert second_downloaded is False
    assert calls == [(3, 6, 3)]


def test_tile_endpoint_returns_cached_png(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    tile_path = tmp_path / "3.png"
    tile_path.write_bytes(PNG_DATA)
    monkeypatch.setattr(map_tiles, "get_cached_tile", lambda z, x, y: (tile_path, False))

    response = TestClient(app).get("/api/map-tiles/3/6/3.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=604800, immutable"
    assert response.content == PNG_DATA
