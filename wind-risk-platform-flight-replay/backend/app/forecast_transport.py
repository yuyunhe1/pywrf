"""Small immutable dataset descriptors for spawned direction processes."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import numpy as np

from .forecast_store import IOStats, LazyArray, Product, store_dir
from .wind_provider import WindGrid
from .wrf_timeline import ForecastDataset


def describe_dataset(dataset):
    frames = []
    temporary = None
    for index, (stamp, grid) in enumerate(dataset.frames):
        from .planning_control import checkpoint
        checkpoint()
        metadata = {name: getattr(grid, name) for name in
                    ("cycle", "forecast_hour", "forecast_minutes", "wind_time_model", "level", "valid_time", "source", "cycle_bj", "valid_time_bj")}
        item = dict(stamp=stamp, metadata=metadata, lons=grid.lons.tolist(), lats=grid.lats.tolist())
        if isinstance(grid.u, LazyArray) and grid.u.product.descriptor:
            item["product"] = grid.u.product.descriptor
        else:
            # In-memory fixtures/other ingestion adapters are frozen to mmap
            # once. Full wind arrays never travel through the process pipe.
            if temporary is None:
                temporary = store_dir() / "transports" / uuid4().hex
                temporary.mkdir(parents=True, exist_ok=False)
            directory = temporary / str(index)
            directory.mkdir()
            for name in ("u", "v", "terrain"):
                value = getattr(grid, name)
                if value is not None:
                    np.save(directory / (name + ".npy"), value, allow_pickle=False)
            item["arrays"] = str(directory)
        frames.append(item)
    body = dict(dataset_id=dataset.dataset_id, cycle=dataset.cycle, level=dataset.level, source=dataset.source,
                coverage_start=dataset.coverage_start, coverage_end=dataset.coverage_end,
                source_hashes=dataset.source_hashes, error=dataset.error, frames=frames, cache_dir=str(store_dir()))
    body["signature"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


def open_dataset(body):
    from . import wrf_cache_provider as wrf, gfs_provider as gfs
    stats, frames = IOStats(), []
    for item in body["frames"]:
        grid = WindGrid(np.asarray(item["lons"]), np.asarray(item["lats"]), None, None, **item["metadata"])
        if "product" in item:
            spec = item["product"]
            path = Path(spec["path"])
            if spec["source"] == "wrf":
                decoder = lambda path=path: wrf._get_grid_cached.__wrapped__(str(path), body["level"], None, (0, 0))
            else:
                pinned = gfs.GfsFile(path, grid.cycle, grid.cycle_bj, grid.forecast_hour, grid.valid_time, grid.valid_time_bj)
                decoder = lambda pinned=pinned: gfs.get_frozen_grid(pinned, body["level"])
                stats.values["full_decode_source_cells_known"] = False
            product = Product(spec["digest"], body["level"], spec["source"], decoder, stats, grid, spec.get("source_cells"))
            product.descriptor = spec
            grid = product.grid()
        else:
            directory = Path(item["arrays"])
            terrain = directory / "terrain.npy"
            grid = replace(grid, u=np.load(directory / "u.npy", mmap_mode="r", allow_pickle=False),
                           v=np.load(directory / "v.npy", mmap_mode="r", allow_pickle=False),
                           terrain=np.load(terrain, mmap_mode="r", allow_pickle=False) if terrain.exists() else None)
        frames.append((item["stamp"], grid))
    return ForecastDataset(body["dataset_id"], body["cycle"], body["level"], tuple(frames), body["coverage_start"],
                           body["coverage_end"], body["error"], body["source"], stats, tuple(body["source_hashes"]))
