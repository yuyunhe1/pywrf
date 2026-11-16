"""Official mirror field selection, byte-range validation and exact GRIB cropping."""

import importlib.util
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.error import URLError

import eccodes as ec
import numpy as np
import pytest


REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("gfs_s3_fallback", REPO / "gfs_s3_fallback.py")
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)


def sample_message():
    handle = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        for key, value in {"Ni": 5, "Nj": 4, "latitudeOfFirstGridPointInDegrees": 4,
                           "latitudeOfLastGridPointInDegrees": 1, "longitudeOfFirstGridPointInDegrees": 1,
                           "longitudeOfLastGridPointInDegrees": 5, "iDirectionIncrementInDegrees": 1,
                           "jDirectionIncrementInDegrees": 1}.items():
            ec.codes_set(handle, key, value)
        ec.codes_set_values(handle, np.arange(20.))
        return ec.codes_get_message(handle)
    finally:
        ec.codes_release(handle)


def test_index_selects_only_requested_fields_and_merges_only_adjacent_records():
    index = '\n'.join(['1:0:d=test:UGRD:100 m above ground:x:',
                       '2:10:d=test:VGRD:100 m above ground:x:',
                       '3:20:d=test:TMP:100 m above ground:x:',
                       '4:30:d=test:HGT:surface:x:', '5:40:d=test:RH:surface:x:'])
    ranges, count = mirror.selected_ranges(index, {'UGRD', 'VGRD', 'HGT'}, {'100 m above ground', 'surface'})
    assert ranges == [(0, 19), (30, 39)]
    assert count == 3
    with pytest.raises(ValueError, match='lacks requested'):
        mirror.selected_ranges(index, {'UGRD', 'VGRD', 'HGT'}, {'100 m above ground', 'surface', '975 mb'})


def test_noaa_url_preserves_time_fields_and_region():
    url = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl?dir=/gfs.20261011/00/atmos&file=gfs.t00z.pgrb2.0p25.f002&var_UGRD=on&var_VGRD=on&var_HGT=on&lev_975_mb=on&subregion=&leftlon=73&bottomlat=18&rightlon=135&toplat=54'
    source, variables, levels, bounds = mirror.selection_from_nomads_url(url)
    assert source.endswith('/gfs.20261011/00/atmos/gfs.t00z.pgrb2.0p25.f002')
    assert variables == {'UGRD', 'VGRD', 'HGT'}
    assert levels == {'975 mb'}
    assert bounds == (73, 18, 135, 54)


def test_crop_preserves_values_and_grib_grid_geometry():
    original = sample_message()
    cropped = list(mirror.crop_payload(original * 2, (2, 2, 4, 3)))
    assert len(cropped) == 2
    handle = ec.codes_new_from_message(cropped[0])
    try:
        assert ec.codes_get(handle, 'Ni') == 3
        assert ec.codes_get(handle, 'Nj') == 2
        assert ec.codes_get(handle, 'numberOfDataPoints') == 6
        assert ec.codes_get(handle, 'latitudeOfFirstGridPointInDegrees') == 3
        assert ec.codes_get(handle, 'latitudeOfLastGridPointInDegrees') == 2
        np.testing.assert_array_equal(ec.codes_get_values(handle), [6, 7, 8, 11, 12, 13])
    finally:
        ec.codes_release(handle)
    assert mirror.crop_message(original, None) == original
    with pytest.raises(ValueError, match='Incomplete'):
        list(mirror.crop_payload(original[:-3], None))


@pytest.mark.parametrize('status,header,payload', [(200, '', b'large global file'), (206, 'bytes 0-3/100', b'GRIB'), (206, 'bytes 10-13/100', b'GR')])
def test_rejects_ignored_wrong_or_truncated_ranges(monkeypatch, status, header, payload):
    class Response(BytesIO):
        pass
    response = Response(payload)
    response.status = status
    response.headers = {'Content-Range': header}
    monkeypatch.setattr(mirror.urllib.request, 'urlopen', lambda *a, **k: response)
    with pytest.raises(ValueError):
        mirror.fetch_range('https://example.test/gfs', (10, 13), 5, 1)


def test_network_reset_switches_to_mirror_and_reuses_it_for_next_file(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('gfs_transport_test', REPO / 'download_gfs_hourly_70vars_realtime.py')
    downloader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(downloader)
    monkeypatch.setitem(sys.modules, 'gfs_s3_fallback', mirror)
    monkeypatch.setenv('GFS_DOWNLOAD_TRANSPORT', 'auto')
    calls = []
    def fail_nomads(*args, **kwargs):
        calls.append('nomads')
        raise URLError(ConnectionResetError('connection reset'))
    def mirror_download(url, path, **kwargs):
        calls.append('s3')
        path.write_bytes(b'GRIBcomplete7777')
    monkeypatch.setattr(downloader.urllib.request, 'urlopen', fail_nomads)
    monkeypatch.setattr(mirror, 'download_from_s3', mirror_download)
    for filename in ['first.grib2', 'second.grib2']:
        assert downloader.download('https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl', tmp_path / filename, retries=1, min_bytes=8)
    assert calls == ['nomads', 's3', 's3']


def test_failed_mirror_preserves_existing_file(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('gfs_failed_transport_test', REPO / 'download_gfs_hourly_70vars_realtime.py')
    downloader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(downloader)
    monkeypatch.setitem(sys.modules, 'gfs_s3_fallback', mirror)
    monkeypatch.setenv('GFS_DOWNLOAD_TRANSPORT', 's3')
    def incomplete(url, path, **kwargs):
        path.write_bytes(b'GRIBtruncated')
        raise ValueError('truncated range')
    monkeypatch.setattr(mirror, 'download_from_s3', incomplete)
    target = tmp_path / 'existing.grib2'
    target.write_bytes(b'GRIBprevious7777')
    assert not downloader.download('https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl', target, retries=1, min_bytes=8)
    assert target.read_bytes() == b'GRIBprevious7777'
    assert not target.with_suffix('.grib2.tmp').exists()
