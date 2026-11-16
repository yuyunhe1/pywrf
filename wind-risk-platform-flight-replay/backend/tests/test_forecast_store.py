"""Cold decode accounting, local mmap slices, and immutable lazy future hours."""
from collections import OrderedDict
from datetime import timedelta
import hashlib

import numpy as np
import pytest

from app import wrf_cache_provider as wrf, wrf_timeline as timeline
from app.forecast_store import LazyArray
from test_temporal_routing import BASE


@pytest.fixture
def source_files(monkeypatch, tmp_path):
    records = []
    for hour in range(4):
        records.append(dict(path=f"hour-{hour}.npz", cycle=BASE.strftime("%Y-%m-%d %H:%M UTC"),
                            valid_time=(BASE + timedelta(hours=hour)).strftime("%Y-%m-%d %H:%M UTC"), forecast_hour=hour))
    def write(hour, value=2.):
        row = records[hour]
        np.savez_compressed(tmp_path / row["path"], lons=np.linspace(0, .6, 25), lats=np.linspace(.2, -.2, 21),
                            levels_m=np.array([100, 200, 300, 350]), u=np.full((4, 21, 25), value), v=np.zeros((4, 21, 25)),
                            terrain=np.ones((21, 25)) * 42., cycle_utc=row["cycle"], valid_time_utc=row["valid_time"], forecast_hour=hour)
    for i in range(4):
        write(i)
    monkeypatch.setattr(wrf, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(wrf, "local_forecast_records", lambda now: (records[0]["cycle"], records))
    monkeypatch.setattr(timeline, "_DATASETS", OrderedDict())
    monkeypatch.setenv("FORECAST_DERIVED_CACHE_DIR", str(tmp_path / "derived"))
    return records, write, tmp_path


def test_late_hour_is_pinned_before_decode_and_old_arrays_survive_replacement(source_files):
    records, write, root = source_files
    originals = {row["path"]: hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() for row in records}
    old = timeline.load_dataset("100m AGL", BASE)
    assert len(old.frames) == 4
    assert all(isinstance(grid.u, LazyArray) for _, grid in old.frames)
    assert old.io_stats.snapshot()["full_decodes"] == 0
    assert [digest for _, digest in old.source_hashes] == list(originals.values())
    assert timeline.load_dataset("100m AGL", BASE) is old
    assert all(hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() == originals[row["path"]] for row in records)
    write(3, 6.)  # future hour had never been decoded when the producer changed it
    new = timeline.load_dataset("100m AGL", BASE)
    assert new.dataset_id != old.dataset_id
    assert old.frames[3][1].u[10, 10] == 2.
    assert new.frames[3][1].u[10, 10] == 6.
    assert old.frames[3][1].terrain[10, 10] == 42.
    # Evict from the twelve-entry replay LRU; an active task's strong reference
    # still pins both the dataset identity and its snapshot files.
    timeline._DATASETS.clear()
    assert timeline.get_dataset(old.dataset_id) is old


def test_cold_full_variable_decode_and_warm_local_slices_are_counted_separately(source_files):
    data = timeline.load_dataset("100m AGL", BASE)
    grid = data.frames[1][1]
    assert grid.u.shape == (21, 25)
    assert grid.u[3:5, 3:6].shape == (2, 3)
    stats = data.io_stats.snapshot()
    assert stats["full_decodes"] == 1
    assert stats["full_decode_cells"] == 2 * 4 * 21 * 25  # compressed U/V include all levels
    assert stats["full_decode_output_cells"] == 2 * 21 * 25
    assert stats["mmap_requested_cells"] == 6
    assert grid.v[3:5, 3:6].sum() == 0
    assert data.io_stats.snapshot()["full_decodes"] == 1
    # A fresh dataset instance reuses the immutable mmap product, with no decode.
    timeline._DATASETS.clear()
    hot = timeline.load_dataset("100m AGL", BASE)
    assert hot.frames[1][1].u[3, 3] == 2.
    assert hot.io_stats.snapshot()["full_decodes"] == 0
    assert hot.io_stats.snapshot()["mmap_reuses"] == 1


def test_lazy_arrays_remain_compatible_with_whole_map_requests(source_files):
    data = timeline.load_dataset("100m AGL", BASE)
    value = timeline.wind_frame(data, BASE.timestamp() + 1800)
    assert value["available"] and len(value["velocity"][0]["data"]) == 21 * 25
    assert set(value["velocity"][0]["data"]) == {2.}
    assert data.io_stats.snapshot()["full_decodes"] == 2
    assert data.frames[0][1].u.product.array("u").flags.writeable is False


def test_explicit_cycle_recovers_archived_hours_missing_from_rolling_index(source_files,monkeypatch):
    from app.temporal_plan_service import prepare_temporal_request
    from test_temporal_routing import request
    records,write,root=source_files
    cycle=records[0]['cycle']
    archive=root/BASE.strftime('%Y%m%d%H')
    archive.mkdir()
    for row in records:
        (root/row['path']).replace(archive/row['path'])
    # Rolling index contains only two hours; all four belong to the selected batch.
    indexed=[{**row,'path':str((archive/row['path']).relative_to(root))} for row in records[2:]]
    monkeypatch.setattr(wrf,'load_index',lambda:{'files':indexed})
    selected,data=prepare_temporal_request(request().model_copy(update={'cycle':cycle}))
    assert selected.cycle==data.cycle==cycle
    assert len(data.frames)==4 and data.frames[0][0]==BASE.timestamp()
    other=timeline.load_dataset('100m AGL',BASE,'2026-11-01 06:00 UTC')
    assert not other.frames  # No silent substitution with the latest batch.


def test_forecast_revision_changes_for_replaced_hour_but_not_unchanged_sync(source_files,monkeypatch):
    from app import main
    records,write,root=source_files
    monkeypatch.setattr(main,'availability',lambda *args,**kwargs:{'cycles':[]})
    monkeypatch.setattr(main.v3_native,'status',lambda:{})
    monkeypatch.setattr(wrf,'load_index',lambda:{'files':records})
    first=main.times(refresh_files=False,auto_download=False,source='wrf')['forecast_revision']
    assert main.times(refresh_files=False,auto_download=False,source='wrf')['forecast_revision']==first
    write(3,6.)
    assert main.times(refresh_files=False,auto_download=False,source='wrf')['forecast_revision']!=first
