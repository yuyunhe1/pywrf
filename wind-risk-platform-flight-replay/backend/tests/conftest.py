"""Pytest bootstrap for importing the local FastAPI app package."""

import sys
import os
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
# Unsatisfiable search fixtures must finish without consuming the host's RAM.
# Resource-policy tests explicitly select the unlimited setting when needed.
os.environ.setdefault("ROUTE_PLAN_MAX_TEMPORAL_LABELS", "180000")
os.environ.setdefault("ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS", "1")
os.environ.setdefault("ROUTE_PLAN_DIRECTION_PROCESSES", "0")  # isolated unit doubles stay in-process
# Legacy numerical fixtures remain explicit; native production defaults to
# strict provenance and native tests enable it individually.
os.environ.setdefault('WRF_NATIVE_10MIN_REQUIRED','0')
os.environ.setdefault('WIND_FRAME_WARMUP','0')  # Synthetic tests never prepare the real platform's cache.
os.environ.setdefault("FORECAST_DERIVED_CACHE_DIR", str(BACKEND_ROOT.parents[1] / "data/segmented_planning_tests/pytest-cache"))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


import pytest


@pytest.fixture(autouse=True)
def isolate_route_persistence(tmp_path,monkeypatch):
    from app import route_storage,main
    monkeypatch.setattr(route_storage,'DB_PATH',tmp_path/'routes.sqlite3')
    exports=tmp_path/'isolated-exports';exports.mkdir(exist_ok=True)
    monkeypatch.setattr(main,'EXPORT_DIR',exports)


@pytest.fixture(autouse=True)
def isolate_synthetic_forecast_blocks():
    # Historical unit fixtures reuse dataset_id='temporal-test' with different
    # arrays; real dataset IDs are immutable. Never share those across tests.
    from app.local_weather import _BLOCKS, _BLOCK_LOCK
    with _BLOCK_LOCK:
        _BLOCKS.clear()
