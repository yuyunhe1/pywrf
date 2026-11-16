from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from app import gfs_provider as gfs
from app import main
from app.wind_provider import WindGrid


def test_vector_interpolation_sorts_profiles_and_preserves_missing_columns():
    heights = np.array([[[500, 500, 500, np.nan]], [[100, 100, 100, np.nan]], [[300, 300, 300, np.nan]]], dtype=float)
    u = heights / 100
    v = u * 2
    u[:, 0, 2] = np.nan
    results = gfs._interpolate_profiles(heights, u, v, [250, 300, 350, 500, 600, 50])
    for height, (out_u, out_v) in zip([250, 300, 350, 500], results):
        np.testing.assert_allclose(out_u[0, :2], height / 100)
        np.testing.assert_allclose(out_v[0, :2], height / 50)
        assert np.isnan(out_u[0, 2:]).all()
    assert np.isnan(results[4][0]).all()
    assert np.isnan(results[5][0]).all()


def pressure_data(heights, u):
    shape = (len(heights), 2, 2)
    coords = {"isobaricInhPa": [1000, 975, 950][:len(heights)], "latitude": [31., 30.], "longitude": [118., 119.]}
    dims = tuple(coords)
    dataset = xr.Dataset({
        "gh": xr.DataArray(np.broadcast_to(np.asarray(heights)[:, None, None], shape), coords=coords, dims=dims),
        "u": xr.DataArray(np.broadcast_to(np.asarray(u)[:, None, None], shape), coords=coords, dims=dims),
        "v": xr.DataArray(np.broadcast_to(np.asarray(u)[:, None, None] * 2, shape), coords=coords, dims=dims),
    })
    surface = dataset.gh.isel(isobaricInhPa=0, drop=True).copy(data=np.zeros((2, 2)))
    return dataset, surface


@pytest.mark.parametrize("available_anchor", [100, 10, None])
def test_high_pressure_profile_uses_native_lower_anchor(monkeypatch, available_anchor):
    dataset, surface = pressure_data([500, 1000], [5, 10])
    monkeypatch.setattr(gfs, "_open_isobaric_dataset", lambda _: dataset)
    monkeypatch.setattr(gfs, "_open_surface_height", lambda _: surface)

    def native(_, height, kind):
        if height != available_anchor:
            raise ValueError("absent")
        return surface.copy(data=np.full((2, 2), height / 100 * (1 if kind == "u" else 2)))

    monkeypatch.setattr(gfs, "_open_height_var", native)
    results = gfs._interpolate_to_agl_heights(Path("sample.grib2"), [250, 300, 350], None)
    for height, (u, v) in zip([250, 300, 350], results):
        if available_anchor is None:
            assert np.isnan(u.values).all()
        else:
            np.testing.assert_allclose(u.values, height / 100)
            np.testing.assert_allclose(v.values, height / 50)


def test_average_components_read_pressure_profile_once(monkeypatch):
    dataset, surface = pressure_data([100, 300, 500], [2, 6, 20])
    reads = []
    monkeypatch.setattr(gfs, "_open_isobaric_dataset", lambda path: reads.append(path) or dataset)
    monkeypatch.setattr(gfs, "_open_surface_height", lambda _: surface)
    item = gfs.GfsFile(Path("sample.grib2"), "test", "test", 1, "test", "test")
    monkeypatch.setattr(gfs, "find_file", lambda *_: item)
    gfs._get_grid_cached.cache_clear()
    try:
        grid = gfs.get_grid("test", 1, gfs.AVERAGE_LAYER, None)
        np.testing.assert_allclose(grid.u, (5 + 4 * 6 + 9.5) / 6)
        np.testing.assert_allclose(grid.v, grid.u * 2)
        assert len(reads) == 1
    finally:
        gfs._get_grid_cached.cache_clear()


@pytest.mark.parametrize("all_missing", [False, True])
def test_missing_grid_values_are_valid_json(monkeypatch, all_missing):
    values = np.array([[np.nan, np.inf], [1., 2.]])
    if all_missing:
        values[:] = np.nan
    grid = WindGrid(lons=np.array([118., 119.]), lats=np.array([31., 30.]), u=values, v=values,
                    cycle="test", forecast_hour=1, level=gfs.AVERAGE_LAYER, valid_time="test")
    monkeypatch.setattr(main, "load_grid", lambda *_: grid)
    client = TestClient(main.app)
    wind = client.get('/api/wind').json()
    assert wind['velocity'][0]['data'][:2] == [None, None]
    response = client.get('/api/heatmap')
    assert response.status_code == 200
    speed = response.json()['wind_speed']
    assert speed['data'][:2] == [None, None]
    assert (speed['min'] is None) == all_missing
    assert (speed['max'] is None) == all_missing


def test_average_layer_representative_altitude():
    from app.multi_altitude_routing import level_height_m
    from app.wind_shear import level_height_m as shear_height
    from app.cost_evaluator import _level_height_agl
    from types import SimpleNamespace
    assert level_height_m(gfs.AVERAGE_LAYER) == 300
    assert shear_height(gfs.AVERAGE_LAYER) == 300
    assert _level_height_agl(SimpleNamespace(level=gfs.AVERAGE_LAYER)) == 300
