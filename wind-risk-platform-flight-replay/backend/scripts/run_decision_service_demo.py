"""Demo for first-version navigation decision service.

Run from repository root:
    python wind-risk-platform/backend/scripts/run_decision_service_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.decision_service import decide_navigation
from app.wind_provider import WindGrid


def grid(speed: float, valid_time: str) -> WindGrid:
    lons = np.arange(7, dtype=float)
    lats = np.arange(6, -1, -1, dtype=float)
    return WindGrid(
        lons=lons,
        lats=lats,
        u=np.full((7, 7), speed, dtype=float),
        v=np.zeros((7, 7), dtype=float),
        cycle="2026-06-15 00:00 UTC",
        forecast_hour=3,
        level="100m AGL",
        valid_time=valid_time,
    )


def main() -> None:
    result = decide_navigation(
        start_point=(0.0, 3.0),
        end_point=(6.0, 3.0),
        candidate_forecast_times=[
            grid(6.0, "2026-06-15 03:00 UTC"),
            grid(5.8, "2026-06-15 04:00 UTC"),
            grid(3.0, "2026-06-15 05:00 UTC"),
        ],
        max_wind_speed_threshold=5.4,
        planner_type="wa_lpa_star",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
