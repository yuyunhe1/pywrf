from dataclasses import replace
from datetime import timedelta
import json
import math
import struct

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main, wind_frames as codec, wrf_timeline as timeline, wrf_cache_provider as wrf
from app.forecast_store import IOStats, snapshot_file, _SNAPSHOTS
from test_native_wrf_time import native_dataset
from test_forecast_store import source_files
from test_temporal_routing import BASE


def unpack(raw):
    signature, length = struct.unpack_from('<8sI', raw)
    assert signature == codec.MAGIC
    meta = json.loads(raw[12:12 + length])
    start = (12 + length + 7) // 8 * 8
    for frame in meta['frames']:
        if frame['available']:
            frame['arrays'] = [np.frombuffer(raw, dtype=part['dtype'], count=part['count'],
                offset=start + part['offset']) for part in frame['components']]
    return meta['frames']


def test_packet_uses_float32_only_when_every_value_survives_exactly():
    data = native_dataset()
    source = timeline.wind_frame_arrays(data, BASE.timestamp())
    source['u'][0, 0] = math.pi  # Must remain Float64.
    source['v'][0, 1] = float(np.float32(1.2345678))  # Exactly representable.
    frame = codec.canonical(source)
    body = unpack(codec.packet([frame]))[0]
    assert [part['dtype'] for part in body['components']] == ['<f8', '<f4']
    for i, name in enumerate(('u', 'v')):
        assert np.array_equal(body['arrays'][i], source[name].ravel(), equal_nan=True)


def test_missing_components_remain_missing_in_both_vectors():
    source = timeline.wind_frame_arrays(native_dataset(), BASE.timestamp())
    source['u'][0, 0] = np.nan
    source['v'][0, 1] = np.inf
    body = unpack(codec.packet([codec.canonical(source)]))[0]
    assert all(np.isnan(array[:2]).all() for array in body['arrays'])


def test_two_frame_packet_preserves_independent_missing_native_frame(monkeypatch):
    data = native_dataset(minutes=[0, 20, 180])
    monkeypatch.setattr(main, 'get_dataset', lambda value: data)
    client = TestClient(main.app)
    response = client.get('/api/wind/frames', params=[('dataset_id', data.dataset_id),
        ('at', BASE.isoformat()), ('at', (BASE + timedelta(minutes=10)).isoformat())])
    assert response.status_code == 200
    frames = unpack(response.content)
    assert [frame['available'] for frame in frames] == [True, False]
    assert frames[1]['metadata']['valid_time'] == timeline.iso(BASE.timestamp() + 600)
    assert '数据不足' in frames[1]['reason']


def test_packet_endpoints_keep_exact_previous_frame_and_cache_only_immutable_identity(monkeypatch):
    data = native_dataset(lambda minute, u, v: u.fill(2 if minute == 0 else 8))
    monkeypatch.setattr(main, 'get_dataset', lambda value: data)
    client = TestClient(main.app)
    def read(seconds, **headers):
        return client.get('/api/wind/frame', params=dict(dataset_id=data.dataset_id,
            at=(BASE + timedelta(seconds=seconds)).isoformat()), headers=headers)
    first, three, nine, ten = [read(value) for value in (0, 180, 599.999, 600)]
    assert all(response.status_code == 200 for response in (first, three, nine, ten))
    assert first.content == three.content == nine.content
    assert unpack(three.content)[0]['arrays'][0][0] == 2
    assert unpack(ten.content)[0]['arrays'][0][0] == 8
    assert first.headers['etag'] == three.headers['etag'] != ten.headers['etag']
    assert 'immutable' in first.headers['cache-control']
    assert read(180, **{'If-None-Match': first.headers['etag']}).status_code == 304
    assert 'content-encoding' not in read(0, **{'Accept-Encoding': 'gzip;q=0.0'}).headers


def test_static_frame_pins_the_selected_batch_and_height(source_files, monkeypatch):
    records, _, root = source_files
    monkeypatch.setattr(wrf, '_find_record', lambda cycle, hour: records[int(hour)] if cycle == records[0]['cycle'] else None)
    monkeypatch.setattr(main, 'forecast_revision', lambda source: 'published')
    client = TestClient(main.app)
    query = dict(source='wrf', cycle=records[0]['cycle'], forecast_minutes=60, level='100m AGL', revision='published')
    response = client.get('/api/wind/frame', params=query)
    assert response.status_code == 200
    frame = unpack(response.content)[0]
    assert frame['metadata']['cycle'] == records[0]['cycle']
    assert frame['metadata']['forecast_minutes'] == 60 and frame['metadata']['level'] == '100m AGL'
    assert set(frame['arrays'][0]) == {2.}
    assert client.get('/api/wind/frame', params={**query, 'revision': 'old'}).status_code == 409
    assert client.get('/api/wind/frame', params={'source': 'wrf', 'cycle': query['cycle']}).status_code == 422


def test_unversioned_static_request_has_revalidation_and_gfs_uses_same_transport(monkeypatch):
    grid = replace(native_dataset().frames[0][1], source='GFS fixture', wind_time_model='HOURLY_UV_TEN_MINUTE_HOLD_V1')
    monkeypatch.setattr(main, 'load_grid', lambda *args: grid)
    response = TestClient(main.app).get('/api/wind/frame', params=dict(source='gfs', cycle=grid.cycle, forecast_minutes=0))
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-cache'
    assert unpack(response.content)[0]['metadata']['source_type'] == 'gfs'


@pytest.mark.parametrize('offsets', [[], [0, 10, 20]])
def test_batch_count_is_bounded(offsets):
    query = [('dataset_id', 'unknown')] + [('at', (BASE + timedelta(minutes=offset)).isoformat()) for offset in offsets]
    assert TestClient(main.app).get('/api/wind/frames', params=query).status_code == 422


def test_v3_stays_on_its_frozen_compatibility_interface():
    response = TestClient(main.app).get('/api/wind/frame', params=dict(dataset_id='v3_' + 'a' * 60, at=BASE.isoformat()))
    assert response.status_code == 409 and '冻结材料' in response.json()['detail']


def test_snapshot_registration_survives_process_memory_eviction_and_detects_source_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv('FORECAST_DERIVED_CACHE_DIR', str(tmp_path / 'derived'))
    source = tmp_path / 'one.npz'
    source.write_bytes(b'first version')
    one, digest = snapshot_file(source, IOStats())
    _SNAPSHOTS.clear()
    warm = IOStats()
    assert snapshot_file(source, warm) == (one, digest)
    assert warm.snapshot()['snapshot_bytes'] == 0
    assert warm.snapshot()['immutable_snapshot_reuses'] == 1
    source.write_bytes(b'next version!')
    two, other_digest = snapshot_file(source, IOStats())
    assert two != one and digest != other_digest and one.read_bytes() == b'first version'


def test_vectorized_average_matches_column_interpolation_with_missing_and_exact_heights():
    rng = np.random.default_rng(54)
    levels = np.array([400., 100., 300., 200.])
    values = rng.normal(size=(4, 8, 9))
    values[0, 1, 2] = np.nan
    values[2, 3, 4] = np.nan
    ordered = np.argsort(levels)
    samples = [np.apply_along_axis(lambda column: np.interp(height, levels[ordered], column), 0, values[ordered])
               for height in (250, 300, 350)]
    expected = (samples[0] + 4 * samples[1] + samples[2]) / 6
    np.testing.assert_array_equal(wrf._average_250_350m(levels, values), expected)
