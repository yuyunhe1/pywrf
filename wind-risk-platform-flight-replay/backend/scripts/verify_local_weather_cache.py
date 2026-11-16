"""Real WRF cold/hot local reads, with compressed decoding reported separately."""
import json
import os
from time import perf_counter
from uuid import uuid4

from verify_segmented_planning import WORK, pinned_dataset
from app.local_weather import LocalWeather
from app.models import RoutePlanRequest
from app.planning_limits import process_peak_memory_mb
from app.route_options import build_strategy_cost_config


def main():
    os.environ["FORECAST_DERIVED_CACHE_DIR"] = str(WORK / "weather-cost" / uuid4().hex[:8])
    start = perf_counter()
    data = pinned_dataset("2026-11-08 00:00 UTC", "100m AGL")
    request = RoutePlanRequest(start=[118.16321, 30.81898], end=[117.59412, 30.92739], level="100m AGL", source="wrf",
                               departure_at="2026-11-08T16:03:00+08:00", time_aware=True)
    report = dict(setup_seconds=perf_counter() - start, phases=[])
    for name in ("cold", "hot"):
        before = data.io_stats.snapshot()
        weather = LocalWeather(data, request, build_strategy_cost_config("wind_avoidance"), loading_mode="local")
        start = perf_counter()
        weather.read_region(request.start, request.departure_at.timestamp())
        # Require both adjacent hourly vectors of the 16:00 held frame, and
        # a further interpolated frame in the same hour (16:10).
        edge = weather.edge(request.start, [118.15521, 30.82298])
        for stamp in (request.departure_at.timestamp(), request.departure_at.timestamp() + 600):
            edge.allowed(stamp)
            weather.cost(edge, stamp)
        after = data.io_stats.snapshot()
        report["phases"].append(dict(phase=name, seconds=perf_counter() - start, weather=weather.diagnostics(),
                                     source_io_delta={k: after[k] - before[k] for k in after if type(after[k]) in (float, int)},
                                     peak_process_memory_mb=process_peak_memory_mb()))
    assert report["phases"][0]["source_io_delta"]["full_decodes"] > 0
    assert report["phases"][1]["source_io_delta"]["full_decodes"] == 0
    assert report["phases"][1]["weather"]["local_block_reads"] == 0
    (WORK / "weather-cache-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
