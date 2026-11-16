"""Real local/public packet checks. Profiles and jobs are ephemeral; no route saves."""
import asyncio
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import re
import struct
import sys
from time import perf_counter
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'data/public_wind_frame_tests'
HTTP = build_opener(ProxyHandler({}))
LOCAL = 'http://127.0.0.1:8001'
RUNTIME_PATH = ROOT / 'data/platform_runtime/v3-running.json'


def fetch(path, body=None, base=LOCAL, method=None, headers=None):
    began = perf_counter()
    request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={'Content-Type': 'application/json', 'Accept-Encoding': 'gzip', **(headers or {})})
    try:
        response = HTTP.open(request, timeout=120)
    except HTTPError as error:
        if error.code != 304: raise
        response = error
    with response:
        first = perf_counter() - began
        wire = response.read()
        raw = gzip.decompress(wire) if wire and response.headers.get('Content-Encoding') == 'gzip' else wire
        return raw, dict(status=response.code, first_byte_seconds=round(first, 5),
            total_seconds=round(perf_counter() - began, 5), wire_bytes=len(wire), raw_bytes=len(raw),
            etag=response.headers.get('ETag'), encoding=response.headers.get('Content-Encoding'),
            cache_control=response.headers.get('Cache-Control'))


def api(path, body=None, **kwargs): return json.loads(fetch(path, body, **kwargs)[0])


def unpack(raw):
    magic, size = struct.unpack_from('<8sI', raw)
    assert magic == b'WFRM001\0'
    description = json.loads(raw[12:12 + size])
    start = (12 + size + 7) // 8 * 8
    for frame in description['frames']:
        if frame['available']:
            frame['arrays'] = [np.frombuffer(raw, dtype=part['dtype'], count=part['count'], offset=start + part['offset'])
                               for part in frame['components']]
    return description['frames']


def frame_path(dataset_id, at):
    return '/api/wind/frame?' + urlencode(dict(dataset_id=dataset_id, at=at))


async def owner_check(public, row, points, report):
    client = 'wind-packet-smoke-' + uuid4().hex
    departure = (datetime.now(timezone.utc) + timedelta(minutes=2)).replace(second=0, microsecond=0)
    body = dict(source='wrf', cycle=row['cycle'], level='100m AGL', start=points[0], end=points[1],
        time_aware=True, departure_at=departure.isoformat(), average_ground_speed_mps=10,
        planner_type='wa_lpa_star', planning_strategy='distance_priority',
        client_instance_id=client, client_request_sequence=1)
    job = await asyncio.to_thread(api, '/api/route/plan/jobs', body)
    path = '/api/route/plan/jobs/' + job['job_id']
    try:
        url = public.replace('https://', 'wss://') + path + '/owner'
        async with websockets.connect(url, proxy=None, open_timeout=40, max_size=None) as socket:
            await socket.send(json.dumps(dict(client_instance_id=client)))
            beat = json.loads(await asyncio.wait_for(socket.recv(), timeout=25))
            assert beat['type'] == 'heartbeat'
            await socket.send(json.dumps(dict(type='ack', seq=beat['seq'])))
            report['public_owner_heartbeat'] = beat['seq']
        began = perf_counter()
        state = await asyncio.to_thread(api, path, method='DELETE')
        while not state['compute_finished'] and perf_counter() - began < 3:
            await asyncio.sleep(.05)
            state = await asyncio.to_thread(api, path)
        assert state['compute_finished'], state
        report['owner_cancel_seconds'] = round(perf_counter() - began, 4)
        report['checks'].append('Production proxy and temporary public owner WebSocket work; the ephemeral job releases its processes')
    finally:
        await asyncio.to_thread(api, path, method='DELETE')
        await asyncio.to_thread(api, '/api/route/planning-clients/' + client + '?through=1&forget=true', method='DELETE')


async def main():
    runtime = json.loads(RUNTIME_PATH.read_text(encoding='utf-8-sig'))
    public = re.findall(r'https://[a-z0-9-]+\.trycloudflare.com', Path(runtime['tunnel_stderr']).read_text(encoding='utf-8'))[0]
    before = api('/api/routes')
    report = dict(checks=[], samples=[], public_url=public, writes_platform_history=False)
    for base in (LOCAL, 'http://127.0.0.1:5174', public):
        health = await asyncio.to_thread(api, '/api/health?source=wrf', base=base)
        assert health['resource_limits']['max_temporal_labels'] == 600000
        assert health['resource_limits']['decoded_grid_cache_mb'] + health['resource_limits']['encoded_frame_cache_mb'] == 64
        report['checks'].append(base + ' serves the unchanged total resource budget')
    release = await asyncio.to_thread(api, '/release.json', base=public)
    assert release['mode'] == 'production'
    html = (await asyncio.to_thread(fetch, '/', base=public))[0].decode()
    assert '/@vite/client' not in html and '/assets/' in html
    report['release'] = release
    report['checks'].append('Temporary public frontend serves the completed production build')
    available = api('/api/times?source=wrf&auto_download=false')
    cycle = max(available['cycles'])
    rows = [row for row in available['valid_times'] if row['cycle'] == cycle]
    assert len(rows) == 289
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    geometry = dict(source='wrf', cycle=cycle, level='100m AGL', average_ground_speed_mps=10,
        points=[[117.17468, 30.91165], [118.13049, 30.39657]], departure_at=(now + timedelta(minutes=2)).isoformat())
    for level in ('100m AGL', '250-350m AGL average'):
        began = perf_counter()
        profile = api('/api/route/flight-profile', {**geometry, 'level': level})
        report.setdefault('profile_preparation_seconds', {})[level] = round(perf_counter() - began, 4)
        dataset = profile['dataset_id']
        if level == '100m AGL':
            (WORK / 'real-profile.json').write_text(json.dumps(profile), encoding='utf-8')
            (WORK / 'real-geometry.json').write_text(json.dumps(geometry), encoding='utf-8')
        for row in rows[60:181:60]:
            stamp = datetime.strptime(row['valid_time'], '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
            legacy = fetch('/api/flight-replay/wind?' + urlencode(dict(dataset_id=dataset, at=stamp.isoformat())),
                           headers={'Accept-Encoding': 'identity'})[0]
            raw, metrics = fetch(frame_path(dataset, stamp.isoformat()))
            frame = unpack(raw)[0]
            expected = json.loads(legacy)
            assert frame['metadata']['dataset_id'] == dataset and frame['metadata']['level'] == level
            for i in (0, 1):
                reference = np.array([np.nan if value is None else value for value in expected['velocity'][i]['data']])
                assert np.array_equal(reference, frame['arrays'][i], equal_nan=True)
            compressed_legacy = len(gzip.compress(legacy, compresslevel=4, mtime=0))
            reduction = 1 - metrics['wire_bytes'] / compressed_legacy
            report['samples'].append(dict(source='wrf', level=level, at=stamp.isoformat(), legacy_gzip_bytes=compressed_legacy,
                reduction=round(reduction, 4), types=[part['dtype'] for part in frame['components']], **metrics))
            if level == '100m AGL': assert reduction >= .5
        stamp = datetime.strptime(rows[84]['valid_time'], '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
        frames = [unpack(fetch(frame_path(dataset, (stamp + timedelta(seconds=offset)).isoformat()))[0])[0]
                  for offset in (0, 180, 599.999, 600)]
        assert frames[0]['metadata']['frame_id'] == frames[1]['metadata']['frame_id'] == frames[2]['metadata']['frame_id']
        assert frames[0]['metadata']['frame_id'] != frames[3]['metadata']['frame_id']
        report['checks'].append(level + ': exact vectors, native hold boundaries and lossless compact transport')
        if level == '100m AGL':
            path = frame_path(dataset, stamp.isoformat())
            local_raw, local_metrics = fetch(path)
            public_raw, public_metrics = await asyncio.to_thread(fetch, path, base=public)
            assert local_raw == public_raw
            conditional = (await asyncio.to_thread(fetch, path, base=public,
                headers={'If-None-Match': public_metrics['etag']}))[1]
            assert conditional['status'] == 304
            report['public_frame'] = public_metrics
            report['public_conditional'] = conditional
            query = [('dataset_id', dataset)] + [('at', (stamp + timedelta(minutes=offset)).isoformat()) for offset in (30, 60)]
            packet, metrics = await asyncio.to_thread(fetch, '/api/wind/frames?' + urlencode(query), base=public)
            pair = unpack(packet)
            assert len(pair) == 2 and all(value['available'] for value in pair)
            report['public_two_frame_packet'] = metrics
            report['checks'].append('Public gzip, ETag/304 and two-frame packet preserve the pinned data')
    gfs = api('/api/times?source=gfs&auto_download=false')
    if not gfs['valid_times']: raise AssertionError('Real GFS regression requires available data')
    row = gfs['valid_times'][len(gfs['valid_times']) // 2]
    query = dict(source='gfs', cycle=row['cycle'], forecast_minutes=round(row['forecast_hour'] * 60), level='100m AGL')
    raw, metrics = fetch('/api/wind/frame?' + urlencode(query))
    frame = unpack(raw)[0]
    assert frame['available'] and frame['metadata']['source_type'] == 'gfs'
    assert frame['metadata']['wind_time_model'] == 'HOURLY_UV_TEN_MINUTE_HOLD_V1'
    report['gfs_single_frame'] = metrics
    report['checks'].append('Real GFS uses the unified transport and its existing time model')
    await owner_check(public, rows[0], [[119.24301281738282, 30.168495178222656], [119.2520128173828, 30.168495178222656]], report)
    assert api('/api/routes') == before
    report['checks'].append('Normal saved route history is unchanged')
    report['warmup'] = api('/api/health?source=wrf')['wind_warmup']
    (WORK / 'real-runtime.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    runtime.update(public_url=public, frontend_release=release, frontend_mode='production',
        backend_health='ok', frontend_proxy_health='ok', public_backend_health='ok',
        planning_runtime_id=available['planning_runtime_id'], verified_at=datetime.now(timezone.utc).isoformat())
    RUNTIME_PATH.write_text(json.dumps(runtime, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__': asyncio.run(main())
