"""Immutable compressed inputs and lazy, native-resolution mmap products.

Snapshotting copies bytes, not decoded weather. A cold NPZ/GRIB decode is
explicitly accounted as a full-variable decode; subsequent slices use mmap.
Products are content addressed and never overwritten or automatically removed.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
from threading import RLock
from time import perf_counter
from uuid import uuid4
from zipfile import ZipFile
from contextlib import contextmanager
import time

import numpy as np
from numpy.lib.mixins import NDArrayOperatorsMixin

from .wind_provider import WindGrid

_LOCK = RLock()
_SNAPSHOTS = {}
_PRODUCT_LOCKS = {}
STORE_SCHEMA = "native-agl-v2-minute-time"


@contextmanager
def process_file_lock(path):
    """OS-owned lock: automatically released if a decoder process is killed."""
    from .planning_control import checkpoint
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            checkpoint()
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                time.sleep(.02)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def store_dir():
    return Path(os.getenv("FORECAST_DERIVED_CACHE_DIR", str(
        Path(__file__).resolve().parents[2] / "data" / "forecast_derived"))).resolve()


class IOStats:
    def __init__(self):
        self.lock = RLock()
        self.values = dict(snapshot_bytes=0, snapshot_seconds=0., full_decode_seconds=0.,
                           full_decode_cells=0, full_decode_output_cells=0, full_decodes=0, mmap_reuses=0,
                           mmap_requested_cells=0, local_block_cells=0, local_block_reads=0,
                           block_cache_hits=0)
        self.values["full_decode_source_cells_known"] = True
        self.values["decode_errors"] = []

    def add(self, **values):
        with self.lock:
            for key, value in values.items():
                self.values[key] = self.values.get(key, 0) + value

    def snapshot(self):
        with self.lock:
            return {key: round(value, 6) if isinstance(value, float) else value
                    for key, value in self.values.items()}


def snapshot_file(path, stats):
    """Pin even hours which have not yet been decoded; detect concurrent writes."""
    path = Path(path).resolve()
    stat = path.stat()
    key = (str(store_dir()), str(path), stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
    with _LOCK:
        if key in _SNAPSHOTS and _SNAPSHOTS[key][0].is_file():
            return _SNAPSHOTS[key]
        root = store_dir() / "sources"
        root.mkdir(parents=True, exist_ok=True)
        reference_key = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        references = root / 'references'
        references.mkdir(exist_ok=True)
        reference_path = references / (reference_key + '.json')
        try:
            reference = json.loads(reference_path.read_text(encoding='utf-8'))
            digest_value = reference['digest']
            if len(digest_value) != 64 or any(letter not in '0123456789abcdef' for letter in digest_value):
                raise ValueError('invalid source digest')
            pinned = root / (digest_value + path.suffix.lower())
            pinned_stat = pinned.stat()
            if (reference['source'] == list(key) and reference['pinned'] ==
                [pinned_stat.st_mtime_ns, pinned_stat.st_size, pinned_stat.st_ctime_ns]):
                stats.add(immutable_snapshot_reuses=1)
                _SNAPSHOTS[key] = (pinned, digest_value)
                return pinned, digest_value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        temporary = root / (uuid4().hex + ".tmp")
        began, digest = perf_counter(), hashlib.sha256()
        try:
            with path.open("rb") as source, temporary.open("wb") as target:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
                    target.write(block)
            after = path.stat()
            if (after.st_mtime_ns, after.st_size, after.st_ctime_ns) != (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns):
                raise ValueError("预报文件在建立快照时更新，请重新发起任务")
            hexdigest = digest.hexdigest()
            target = root / (hexdigest + path.suffix.lower())
            if not target.exists():
                temporary.replace(target)
            else:
                with target.open('rb') as existing:
                    if hashlib.file_digest(existing, 'sha256').hexdigest() != hexdigest:
                        raise ValueError('不可变预报快照校验失败，请检查本地缓存')
            pinned_stat = target.stat()
            reference = dict(source=list(key), digest=hexdigest,
                pinned=[pinned_stat.st_mtime_ns, pinned_stat.st_size, pinned_stat.st_ctime_ns])
            reference_temp = reference_path.with_name(uuid4().hex + '.tmp')
            try:
                reference_temp.write_text(json.dumps(reference), encoding='utf-8')
                reference_temp.replace(reference_path)
            finally:
                reference_temp.unlink(missing_ok=True)
            stats.add(snapshot_bytes=stat.st_size, snapshot_seconds=perf_counter() - began)
            _SNAPSHOTS[key] = (target, hexdigest)
            while len(_SNAPSHOTS) > 4096:
                _SNAPSHOTS.pop(next(iter(_SNAPSHOTS)))
            return target, hexdigest
        finally:
            temporary.unlink(missing_ok=True)


class LazyArray(NDArrayOperatorsMixin):
    """Array-compatible proxy: metadata does not force a weather decode."""
    def __init__(self, product, variable, shape):
        self.product, self.variable, self.shape = product, variable, shape
        self.ndim, self.dtype = len(shape), np.dtype(float)

    @property
    def size(self):
        return int(np.prod(self.shape))

    def __len__(self):
        return self.shape[0]

    def __getitem__(self, key):
        result = self.product.array(self.variable)[key]
        self.product.stats.add(mmap_requested_cells=int(np.size(result)))
        return result

    def __array__(self, dtype=None, copy=None):
        result = np.asarray(self.product.array(self.variable), dtype=dtype)
        self.product.stats.add(mmap_requested_cells=result.size)
        return result.copy() if copy else result

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        return getattr(ufunc, method)(*(np.asarray(x) if isinstance(x, LazyArray) else x for x in inputs), **kwargs)


class Product:
    def __init__(self, source_hash, level, source, decoder, stats, metadata=None, source_cells=None):
        key = hashlib.sha256(json.dumps([STORE_SCHEMA, source, source_hash, level]).encode()).hexdigest()
        self.directory = store_dir() / "fields" / key
        self.decoder, self.stats, self.metadata = decoder, stats, metadata
        self.source_cells = source_cells
        self.descriptor = None
        self.error = None
        self.arrays, self.ready = {}, False
        with _LOCK:
            self.lock = _PRODUCT_LOCKS.setdefault(str(self.directory), RLock())

    def ensure(self):
        if self.ready:
            return
        with self.lock, process_file_lock(self.directory.with_suffix('.lock')):
            if self.ready:
                return
            manifest = self.directory / "grid.json"
            if not manifest.is_file():
                began = perf_counter()
                try:
                    grid = self.decoder()
                    if grid.u.shape != grid.v.shape or grid.u.shape != (len(grid.lats), len(grid.lons)):
                        raise ValueError("预报变量维度不一致")
                    if self.metadata is not None:
                        if not np.array_equal(grid.lons, self.metadata.lons) or not np.array_equal(grid.lats, self.metadata.lats):
                            raise ValueError("同一预报批次的空间网格不一致，不能拼接")
                        if grid.cycle != self.metadata.cycle or grid.valid_time != self.metadata.valid_time:
                            raise ValueError("预报文件时间与版本登记不一致")
                except (OSError, ValueError, KeyError, EOFError) as exc:
                    if self.metadata is None:
                        raise
                    self.error, self.ready = str(exc), True
                    self.stats.add(failed_decodes=1, failed_decode_seconds=perf_counter() - began)
                    with self.stats.lock:
                        self.stats.values["decode_errors"].append(self.error)
                    return
                self.stats.add(full_decodes=1, full_decode_seconds=perf_counter() - began,
                               full_decode_cells=self.source_cells or 0,
                               full_decode_output_cells=int(grid.u.size + grid.v.size))
                temp = self.directory.with_name(self.directory.name + "-" + uuid4().hex)
                temp.mkdir(parents=True, exist_ok=False)
                try:
                    for name in ("lons", "lats", "u", "v", "terrain"):
                        value = getattr(grid, name)
                        if value is not None:
                            np.save(temp / (name + ".npy"), value, allow_pickle=False)
                    meta = {name: getattr(grid, name) for name in
                            ("cycle", "forecast_hour", "forecast_minutes", "wind_time_model", "level", "valid_time", "source", "cycle_bj", "valid_time_bj")}
                    (temp / "grid.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                    temp.replace(self.directory)
                finally:
                    if temp.exists():
                        shutil.rmtree(temp)
            else:
                self.stats.add(mmap_reuses=1)
            self.ready = True

    def array(self, name):
        self.ensure()
        with self.lock:
            if self.error:
                if name in ("lons", "lats"):
                    return getattr(self.metadata, name)
                if name not in self.arrays:
                    self.arrays[name] = np.full((len(self.metadata.lats), len(self.metadata.lons)), np.nan)
                return self.arrays[name]
            if name not in self.arrays:
                path = self.directory / (name + ".npy")
                if name == "terrain" and not path.exists():
                    # Missing terrain does not turn all wind into a full read.
                    return np.zeros(self.array("u").shape)
                self.arrays[name] = np.load(path, mmap_mode="r", allow_pickle=False)
            return self.arrays[name]

    def grid(self):
        if self.metadata is None:
            self.ensure()  # GFS needs only its first hour decoded for axes.
            meta = json.loads((self.directory / "grid.json").read_text(encoding="utf-8"))
            self.metadata = WindGrid(self.array("lons"), self.array("lats"), None, None, **meta)
        grid = self.metadata
        shape = (len(grid.lats), len(grid.lons))
        return replace(grid, u=LazyArray(self, "u", shape), v=LazyArray(self, "v", shape),
                       terrain=LazyArray(self, "terrain", shape))


def wrf_product(record, level, stats):
    from . import wrf_cache_provider as wrf
    from .wrf_native_schema import npz_metadata
    path, digest = snapshot_file(wrf.cache_dir() / record["path"], stats)
    with np.load(path, allow_pickle=False) as data:
        levels = np.asarray(data["levels_m"])
        normalized = wrf.AVERAGE_LAYER if level.lower() == wrf.AVERAGE_LAYER.lower() else wrf._level_index(levels, level)[0]
        metadata=npz_metadata(data)
        grid = WindGrid(np.asarray(data["lons"], dtype=float), np.asarray(data["lats"], dtype=float), None, None,
                        str(data["cycle_utc"].item()), metadata.pop('forecast_hour'), normalized,
                        str(data["valid_time_utc"].item()), source="WRF immutable cache",**metadata)
        if record.get('sha256') and record['sha256'] != digest:
            raise ValueError('WRF 缓存哈希与已发布索引不一致')
        # Accessing files lists no full compressed U/V is decoded here.
        if not {"u", "v"}.issubset(data.files):
            raise ValueError("WRF 缺少 U/V 风变量")
    source_cells = 0
    with ZipFile(path) as archive:
        for name in ("u.npy", "v.npy"):
            with archive.open(name) as stream:
                version = np.lib.format.read_magic(stream)
                shape, _, _ = np.lib.format._read_array_header(stream, version)
                source_cells += int(np.prod(shape))
    product = Product(digest, level, "wrf", lambda: wrf._get_grid_cached.__wrapped__(str(path), level, None, (0, 0)), stats, grid, source_cells)
    product.descriptor = dict(source="wrf", path=str(path), digest=digest, source_cells=source_cells)
    return product.grid(), digest


def gfs_product(item, level, stats, reference=None):
    from . import gfs_provider as gfs
    # ecCodes/height interpolation can decode several pressure variables. Its
    # elapsed cost is measured, but do not invent a raw decoded-cell count.
    stats.values["full_decode_source_cells_known"] = False
    path, digest = snapshot_file(item.path, stats)
    pinned = replace(item, path=path)
    meta = None if reference is None else replace(reference, cycle=item.cycle, valid_time=item.valid_time,
                                                 forecast_hour=item.forecast_hour, cycle_bj=item.cycle_bj,
                                                 valid_time_bj=item.valid_time_bj)
    product = Product(digest, level, "gfs", lambda: gfs.get_frozen_grid(pinned, level), stats, meta)
    product.descriptor = dict(source="gfs", path=str(path), digest=digest)
    return product.grid(), digest


def write_manifest(dataset):
    directory = store_dir() / "datasets"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (dataset.dataset_id + ".json")
    if target.exists():
        return
    value = {"dataset_id": dataset.dataset_id, "source": dataset.source, "cycle": dataset.cycle,
             "level": dataset.level, "derived_schema": STORE_SCHEMA,"wind_time_model":dataset.time_model,
             "sources": [{"valid_time": time, "sha256": digest} for time, digest in dataset.source_hashes]}
    temporary = directory / (uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
