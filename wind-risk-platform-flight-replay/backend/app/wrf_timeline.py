"""Frozen hourly forecast datasets shared by planning, decisions and replay."""
from __future__ import annotations

from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from threading import RLock
from weakref import WeakValueDictionary
from zipfile import BadZipFile

import numpy as np

from . import wrf_cache_provider as wrf
from . import gfs_provider as gfs
from .wind_provider import WindGrid
from .wind_time import frame_time, WIND_TIME_STEP_SECONDS, WIND_TIME_MODEL
from .forecast_store import IOStats, wrf_product, gfs_product, write_manifest
from .grid_coverage import point_cells, SPATIAL_RISK_MODEL
from .wrf_native_schema import NATIVE_WRF_TIME_MODEL, native_required, validate_record


def epoch(label: str) -> float:
    return datetime.strptime(label, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).timestamp()


def iso(stamp: float | None) -> str | None:
    if stamp is None or not math.isfinite(stamp):
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ForecastDataset:
    dataset_id: str
    cycle: str | None
    level: str
    frames: tuple[tuple[float, WindGrid], ...]
    coverage_start: float | None
    coverage_end: float | None
    error: str | None = None
    source: str = "wrf"
    io_stats: object = None
    source_hashes: tuple = ()

    @property
    def time_model(self):
        return self.frames[0][1].wind_time_model if self.frames else (NATIVE_WRF_TIME_MODEL if self.source=='wrf' else WIND_TIME_MODEL)


_DATASETS: OrderedDict[str, ForecastDataset] = OrderedDict()
_LOCK = RLock()
_LIVE_DATASETS = WeakValueDictionary()


def load_dataset(level: str, now: datetime, cycle: str | None = None) -> ForecastDataset:
    """Pin file revisions, one forecast cycle and one height for an evaluation.

    Old arrays remain available for a replay after the producer atomically
    replaces a file. A replay never silently reads a newer revision.
    """
    record_failure=None
    try:
        if cycle is None:
            cycle, records = wrf.local_forecast_records(now)
        else:
            records = wrf.records_for_cycle(cycle)
        if native_required():
            for record in records: validate_record(record)
    except (OSError, ValueError, KeyError) as exc:
        records=[];record_failure=str(exc)
    revisions = []
    for record in records:
        try:
            stat = (wrf.cache_dir() / record["path"]).stat()
            revisions.append((stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns))
        except OSError:
            revisions.append(None)
    signature = [NATIVE_WRF_TIME_MODEL if native_required() else WIND_TIME_MODEL, SPATIAL_RISK_MODEL, str(wrf.cache_dir()), cycle, level,
                 [(r["path"], r["valid_time"], r["forecast_hour"], r.get('sha256'), v) for r, v in zip(records, revisions)]]
    identifier = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:24]
    with _LOCK:
        if identifier in _DATASETS:
            _DATASETS.move_to_end(identifier)
            return _DATASETS[identifier]
        frames, hashes, failures, io = [], [], [record_failure] if record_failure else [], IOStats()
        reference = None
        for record, revision in zip(records, revisions):
            if revision is None:
                continue
            try:
                grid, digest = wrf_product(record, level, io)
                after = (wrf.cache_dir() / record["path"]).stat()
                if (after.st_mtime_ns, after.st_size, after.st_ctime_ns) != revision:
                    continue
                if grid.cycle != cycle or grid.valid_time != record["valid_time"]:
                    continue
                if grid.u.shape != grid.v.shape or grid.u.shape != (len(grid.lats), len(grid.lons)):
                    continue
                if len(grid.lats) < 2 or len(grid.lons) < 2:
                    continue
                if reference is not None and (not np.array_equal(grid.lons, reference.lons)
                                               or not np.array_equal(grid.lats, reference.lats)):
                    continue
                reference = grid
                frames.append((epoch(record["valid_time"]), grid))
                hashes.append((record["valid_time"], digest))
            except (OSError, ValueError, KeyError, EOFError, BadZipFile) as exc:
                failures.append(str(exc))
                continue
        times = [epoch(record["valid_time"]) for record in records]
        dataset = ForecastDataset(identifier, cycle, level, tuple(sorted(frames, key=lambda item: item[0])),
                                  min(times) if times else None, max(times) if times else None,
                                  None if frames else "该批次缺少可用的原生 10 分钟 WRF 风场，等待同步后重新评估：" + "; ".join(failures[:2]),
                                  io_stats=io, source_hashes=tuple(hashes))
        _DATASETS[identifier] = dataset
        write_manifest(dataset)
        _LIVE_DATASETS[identifier] = dataset
        while len(_DATASETS) > 12:
            _DATASETS.popitem(last=False)
        return dataset


def load_gfs_dataset(level: str, now: datetime, cycle: str | None = None) -> ForecastDataset:
    """Pin one GFS cycle and file revisions, preserving the map's native grid."""
    files = gfs.discover_files()
    cycle = cycle or max((item.cycle for item in files if epoch(item.cycle) <= now.timestamp()), default=None)
    records = [item for item in files if cycle is not None and item.cycle == cycle]
    revisions = []
    for item in records:
        try:
            stat = item.path.stat()
            revisions.append((stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns))
        except OSError:
            revisions.append(None)
    signature = [WIND_TIME_MODEL, SPATIAL_RISK_MODEL, "gfs", cycle, level, [(str(item.path), item.valid_time, revision) for item, revision in zip(records, revisions)]]
    identifier = "gfs_" + hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:24]
    with _LOCK:
        if identifier in _DATASETS:
            _DATASETS.move_to_end(identifier)
            return _DATASETS[identifier]
        frames, hashes, failures, io, reference = [], [], [], IOStats(), None
        for item, revision in zip(records, revisions):
            if revision is None:
                continue
            try:
                grid, digest = gfs_product(item, level, io, reference)
                after = item.path.stat()
                if (after.st_mtime_ns, after.st_size, after.st_ctime_ns) != revision or grid.cycle != cycle or grid.valid_time != item.valid_time:
                    continue
                if grid.u.shape != grid.v.shape or grid.u.shape != (len(grid.lats), len(grid.lons)) or min(grid.u.shape) < 2:
                    continue
                if reference is not None and (not np.array_equal(grid.lons, reference.lons) or not np.array_equal(grid.lats, reference.lats)):
                    continue
                reference = grid
                frames.append((epoch(item.valid_time), grid))
                hashes.append((item.valid_time, digest))
            except (OSError, ValueError, KeyError, EOFError) as exc:
                failures.append(str(exc))
                continue
        times = [epoch(item.valid_time) for item in records]
        dataset = ForecastDataset(identifier, cycle, level, tuple(sorted(frames, key=lambda item: item[0])),
                                  min(times) if times else None, max(times) if times else None,
                                  None if frames else "缺少可用的 GFS 小时风场，请检查下载数据和高度层：" + "; ".join(failures[:2]), source="gfs",
                                  io_stats=io, source_hashes=tuple(hashes))
        _DATASETS[identifier] = dataset
        write_manifest(dataset)
        _LIVE_DATASETS[identifier] = dataset
        while len(_DATASETS) > 12:
            _DATASETS.popitem(last=False)
        return dataset


def get_dataset(identifier: str) -> ForecastDataset:
    with _LOCK:
        if identifier not in _DATASETS:
            if identifier in _LIVE_DATASETS:
                return _LIVE_DATASETS[identifier]
            raise ValueError("该次风场评估数据已过期，请刷新通航窗口后重新评估")
        _DATASETS.move_to_end(identifier)
        return _DATASETS[identifier]


def bracket(dataset: ForecastDataset, stamp: float):
    """Use the preceding native frame; only legacy/GFS can blend hourly U/V."""
    times = [item[0] for item in dataset.frames]
    if not times or stamp < times[0] or stamp > times[-1]:
        return None
    stamp = frame_time(stamp)
    index = bisect_left(times, stamp)
    if index < len(times) and abs(times[index] - stamp) < 1e-6:
        return dataset.frames[index][1], dataset.frames[index][1], 0.0
    if dataset.time_model == NATIVE_WRF_TIME_MODEL:
        return None  # A missing native frame is never synthesized from neighbours.
    if index == 0 or index == len(times) or not math.isclose(times[index] - times[index - 1], 3600, abs_tol=.001):
        return None
    return dataset.frames[index - 1][1], dataset.frames[index][1], (stamp - times[index - 1]) / 3600


def vector_at(dataset: ForecastDataset, stamp: float, lon: float, lat: float):
    pair = bracket(dataset, stamp)
    if pair is None:
        return None
    left, right, alpha = pair
    if not min(left.lons) <= lon <= max(left.lons) or not min(left.lats) <= lat <= max(left.lats):
        return None
    vectors = [cell_vector(dataset, stamp, row, col) for row,col in point_cells(left,(lon,lat))]
    if not vectors or any(vector is None for vector in vectors):
        return None
    # A closed-cell contact uses the worst wind, never an average that could
    # cancel opposing vectors or hide a risky side cell from the route curve.
    return max(vectors,key=lambda vector: math.hypot(*vector))


def cell_vector(dataset: ForecastDataset, stamp: float, row: int, col: int):
    pair = bracket(dataset, stamp)
    if pair is None:
        return None
    left, right, alpha = pair
    values = (float(left.u[row, col]), float(left.v[row, col]),
              float(right.u[row, col]), float(right.v[row, col]))
    if not all(map(math.isfinite, values)):
        return None
    return values[0] + alpha * (values[2] - values[0]), values[1] + alpha * (values[3] - values[1])


def wind_frame_arrays(dataset: ForecastDataset, stamp: float):
    """Full-area, single-time vectors. Preserve precision for client interpolation."""
    pair = bracket(dataset, stamp)
    label = dataset.source.upper()
    meta = {"dataset_id": dataset.dataset_id, "cycle": dataset.cycle, "level": dataset.level,
            "valid_time": iso(frame_time(stamp)), "requested_time": iso(stamp),
            "wind_time_step_seconds": WIND_TIME_STEP_SECONDS, "wind_time_model": dataset.time_model,
            "coverage_start":iso(dataset.coverage_start),"coverage_end":iso(dataset.coverage_end),
            "source": f"{label} · 飞行回放", "unit": "m/s"}
    if pair is None:
        return {"metadata": meta, "available": False, "reason": f"该时刻 {label} 数据不足，无法显示风场", "velocity": []}
    left, right, alpha = pair
    u = left.u + alpha * (right.u - left.u) if alpha else left.u
    v = left.v + alpha * (right.v - left.v) if alpha else left.v
    valid = np.isfinite(u) & np.isfinite(v)
    if not np.any(valid):
        errors = dataset.io_stats.snapshot().get("decode_errors", []) if dataset.io_stats else []
        return {"metadata": meta, "available": False, "reason": f"该时刻 {label} 变量缺失或没有有效风矢量" +
                ("：" + "; ".join(errors[:2]) if errors else ""), "velocity": []}
    header = {"parameterUnit": "m.s-1", "parameterCategory": 2,
              "nx": len(left.lons), "ny": len(left.lats), "lo1": float(left.lons[0]),
              "la1": float(left.lats[0]), "lo2": float(left.lons[-1]), "la2": float(left.lats[-1]),
              "dx": abs(float(left.lons[1] - left.lons[0])), "dy": abs(float(left.lats[0] - left.lats[1])),
              "refTime": iso(frame_time(stamp))}
    # Cached products normally run north to south; normalize test/other caches too.
    if left.lats[0] < left.lats[-1]:
        u, v, valid = u[::-1], v[::-1], valid[::-1]
        header.update(la1=float(left.lats[-1]), la2=float(left.lats[0]))
    meta.update(bbox=[float(min(left.lons)), float(min(left.lats)), float(max(left.lons)), float(max(left.lats))],
                forecast_hour=(frame_time(stamp) - epoch(dataset.cycle)) / 3600,
                forecast_minutes=round((frame_time(stamp)-epoch(dataset.cycle))/60))
    return {"metadata": meta, "available": True, "header": header,
            "u": np.where(valid, u, np.nan), "v": np.where(valid, v, np.nan)}


def wind_frame(dataset: ForecastDataset, stamp: float) -> dict:
    """Compatibility JSON path; binary display avoids creating Python float lists."""
    frame = wind_frame_arrays(dataset, stamp)
    if not frame['available']:
        return frame
    arrays = []
    for name in ('u', 'v'):
        value = frame[name].ravel().astype(object)
        value[~np.isfinite(frame[name].ravel())] = None
        arrays.append(value.tolist())
    return {"metadata": frame['metadata'], "available": True,
            "velocity": [{"header": {**frame['header'], "parameterNumber": number}, "data": data}
                         for number, data in zip((2, 3), arrays)]}
