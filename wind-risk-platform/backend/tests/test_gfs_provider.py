import numpy as np
import xarray as xr

from app.gfs_provider import MAX_GRID_POINTS, normalize_grid


def test_normalize_grid_reorders_latitude_and_wraps_longitude():
    lats = [30.0, 31.0, 32.0]
    lons = [358.0, 359.0, 0.0, 1.0]
    values = np.arange(12, dtype=float).reshape(3, 4)
    u = xr.DataArray(values, coords={"latitude": lats, "longitude": lons}, dims=("latitude", "longitude"))
    v = xr.DataArray(values + 100, coords={"latitude": lats, "longitude": lons}, dims=("latitude", "longitude"))

    normalized_lons, normalized_lats, normalized_u, normalized_v = normalize_grid(u, v, max_points=100)

    assert normalized_lons.tolist() == [-2.0, -1.0, 0.0, 1.0]
    assert normalized_lats.tolist() == [32.0, 31.0, 30.0]
    assert normalized_u.shape == normalized_v.shape == (3, 4)
    assert normalized_u[0, 0] == values[2, 0]


def test_normalize_grid_crops_and_downsamples_regular_grid():
    lats = np.arange(34.0, 28.9, -0.25)
    lons = np.arange(116.0, 121.1, 0.25)
    values = np.ones((len(lats), len(lons)))
    u = xr.DataArray(values, coords={"latitude": lats, "longitude": lons}, dims=("latitude", "longitude"))
    v = xr.DataArray(values * 2, coords={"latitude": lats, "longitude": lons}, dims=("latitude", "longitude"))

    normalized_lons, normalized_lats, normalized_u, _ = normalize_grid(
        u, v, bbox=(117.0, 30.0, 120.0, 33.0), max_points=25
    )

    assert normalized_u.size <= 25
    assert normalized_lons[0] >= 117.0 and normalized_lons[-1] <= 120.0
    assert normalized_lats[0] <= 33.0 and normalized_lats[-1] >= 30.0
    assert np.all(np.diff(normalized_lons) > 0)
    assert np.all(np.diff(normalized_lats) < 0)


def test_normalize_global_grid_downsamples_to_browser_limit():
    lats = np.arange(90.0, -90.01, -0.25)
    lons = np.arange(0.0, 360.0, 0.25)
    values = np.ones((len(lats), len(lons)), dtype=np.float32)
    u = xr.DataArray(values, coords={"latitude": lats, "longitude": lons}, dims=("latitude", "longitude"))
    v = xr.DataArray(values * 2, coords={"latitude": lats, "longitude": lons}, dims=("latitude", "longitude"))

    normalized_lons, normalized_lats, normalized_u, _ = normalize_grid(u, v, max_points=60000)

    assert normalized_u.size <= 60000
    assert normalized_lons[0] == -180.0
    assert normalized_lons[-1] < 180.0
    assert normalized_lats[0] == 90.0
    assert normalized_lats[-1] == -90.0


def test_default_grid_limit_preserves_source_resolution():
    assert MAX_GRID_POINTS <= 0
    lats = np.arange(90.0, -90.01, -0.25)
    lons = np.arange(0.0, 360.0, 0.25)
    values = np.ones((len(lats), len(lons)), dtype=np.float32)
    u = xr.DataArray(values, coords={"latitude": lats, "longitude": lons}, dims=("latitude", "longitude"))
    v = xr.DataArray(values, coords={"latitude": lats, "longitude": lons}, dims=("latitude", "longitude"))

    normalized_lons, normalized_lats, normalized_u, _ = normalize_grid(u, v)

    assert normalized_u.shape == (721, 1440)
    assert np.isclose(normalized_lons[1] - normalized_lons[0], 0.25)
    assert np.isclose(normalized_lats[0] - normalized_lats[1], 0.25)
