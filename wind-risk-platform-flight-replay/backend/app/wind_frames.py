"""Lossless, bounded weather packets. No temporal model or risk calculation lives here."""
from dataclasses import dataclass
import gzip
import hashlib
import json
import struct

import numpy as np

from .wind_cache import bounded_grid_cache, encoded_cache_budget
from .wrf_timeline import iso, epoch

MIME = 'application/vnd.wind-frame.v1'
MAGIC = b'WFRM001\0'


def from_grid(grid, metadata):
    meta = dict(metadata, valid_time=iso(epoch(grid.valid_time)),
                forecast_minutes=round(grid.forecast_hour * 60), source_type='wrf' if grid.source.startswith('WRF') else 'gfs')
    header = dict(parameterUnit='m.s-1', parameterCategory=2, nx=len(grid.lons), ny=len(grid.lats),
                  lo1=float(grid.lons[0]), la1=float(grid.lats[0]), lo2=float(grid.lons[-1]), la2=float(grid.lats[-1]),
                  dx=abs(float(grid.lons[1] - grid.lons[0])), dy=abs(float(grid.lats[0] - grid.lats[1])),
                  refTime=meta['valid_time'])
    u, v = np.asarray(grid.u), np.asarray(grid.v)
    if grid.lats[0] < grid.lats[-1]:
        u, v = u[::-1], v[::-1]
        header.update(la1=float(grid.lats[-1]), la2=float(grid.lats[0]))
    valid = np.isfinite(u) & np.isfinite(v)
    return canonical(dict(metadata=meta, header=header, available=bool(np.any(valid)),
                          reason='该时刻没有有效风矢量' if not np.any(valid) else None,
                          u=np.where(valid, u, np.nan), v=np.where(valid, v, np.nan)))


def canonical(frame):
    """One held frame is immutable even when requested at 10:03 or 10:09."""
    result = dict(frame)
    meta = {key: value for key, value in frame['metadata'].items() if key != 'requested_time'}
    result['metadata'] = meta
    identity = {key: meta.get(key) for key in ('cycle', 'level', 'valid_time', 'wind_time_model')}
    identity.update(header=frame.get('header'), available=frame['available'])
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode())
    if frame['available']:
        for name in ('u', 'v'):
            array = np.asarray(frame[name], dtype='<f8')
            digest.update(np.where(np.isfinite(array), array, np.nan).tobytes())
    else:
        digest.update(str(frame.get('reason', '')).encode())
    meta['frame_id'] = digest.hexdigest()
    return result


def packet(frames):
    descriptions, chunks, offset = [], [], 0
    for frame in frames:
        description = dict(metadata=frame['metadata'], available=frame['available'], reason=frame.get('reason'))
        if frame['available']:
            description.update(header=frame['header'], components=[])
            valid = np.isfinite(frame['u']) & np.isfinite(frame['v'])
            for number, name in ((2, 'u'), (3, 'v')):
                values = np.where(valid, frame[name], np.nan).astype('<f8').ravel()
                with np.errstate(over='ignore', invalid='ignore'):
                    smaller = values.astype('<f4')
                if np.array_equal(smaller.astype('<f8'), values, equal_nan=True):
                    values = smaller
                data = values.tobytes()
                description['components'].append(dict(dtype=values.dtype.str, count=values.size,
                    offset=offset, parameterNumber=number))
                padding = (-len(data)) % 8
                chunks.append(data + b'\0' * padding)
                offset += len(data) + padding
        descriptions.append(description)
    header = json.dumps(dict(version=1, frames=descriptions), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    prefix = struct.pack('<8sI', MAGIC, len(header)) + header
    return prefix + b'\0' * ((-len(prefix)) % 8) + b''.join(chunks)


@dataclass(frozen=True)
class Encoded:
    body: bytes
    compressed: bytes
    etag: str


def _encode(_key, frames):
    body = packet(frames)
    return Encoded(body, gzip.compress(body, compresslevel=4, mtime=0), hashlib.sha256(body).hexdigest())


# Input arrays are deliberately excluded from keys, so eviction releases them.
_caches = {source: bounded_grid_cache(encoded_cache_budget(), maxsize=64,
    size_of=lambda value: len(value.body) + len(value.compressed), key_of=lambda args, kwargs: args[0])(_encode)
    for source in ('wrf', 'gfs')}


def encode(frames, source):
    key = tuple((frame['metadata']['frame_id'], json.dumps(frame['metadata'], sort_keys=True), frame.get('reason')) for frame in frames)
    return _caches[source](key, frames)


def cache_stats():
    return {name: cache.cache_stats() for name, cache in _caches.items()}
