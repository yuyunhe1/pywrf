"""Select the real GFS provider by default, with an explicit mock mode."""

import os

from . import gfs_downloader, gfs_provider, wind_provider, wrf_cache_provider


def data_mode(source: str | None = None) -> str:
    """Return the requested data mode: real/GFS, wrf_cache/WRF, or mock."""
    mode = (source or os.getenv("GFS_DATA_MODE", os.getenv("WIND_DATA_MODE", "real"))).strip().lower()
    aliases = {
        "gfs": "real",
        "real": "real",
        "wrf": "wrf_cache",
        "wrf_cache": "wrf_cache",
        "mock": "mock",
    }
    if mode not in aliases:
        raise ValueError("source must be gfs, wrf or mock")
    return aliases[mode]
    return mode


def availability(source: str | None = None, auto_download: bool = False) -> dict:
    """Return available selections for the active provider."""
    mode = data_mode(source)
    if mode == "mock":
        valid_times = []
        forecast_hours_by_cycle = {}
        for cycle in wind_provider.CYCLES:
            for hour in wind_provider.FORECAST_HOURS:
                grid = wind_provider.get_grid(cycle, hour, wind_provider.LEVELS[0], None)
                if not gfs_provider.is_selectable_valid_time(grid.valid_time):
                    continue
                forecast_hours_by_cycle.setdefault(grid.cycle, []).append(grid.forecast_hour)
                valid_times.append(
                    {
                        "label": grid.valid_time_bj,
                        "valid_time": grid.valid_time,
                        "cycle": grid.cycle,
                        "cycle_bj": grid.cycle_bj,
                        "forecast_hour": grid.forecast_hour,
                    }
                )
        return {
            "cycles": sorted(forecast_hours_by_cycle),
            "forecast_hours": sorted({hour for hours in forecast_hours_by_cycle.values() for hour in hours}),
            "forecast_hours_by_cycle": {
                cycle: sorted(set(hours)) for cycle, hours in forecast_hours_by_cycle.items()
            },
            "valid_times": sorted(valid_times, key=lambda item: (item["valid_time"], item["cycle"])),
            "levels": [*wind_provider.LEVELS, gfs_provider.AVERAGE_LAYER],
            "domain_bbox": wind_provider.DEFAULT_BBOX,
            "source": "GFS mock",
        }
    if mode == "wrf_cache":
        result = wrf_cache_provider.availability()
        return {**result, "domain_bbox": None, "source": "WRF cache"}
    result = gfs_provider.availability()
    download = gfs_downloader.status()
    has_realtime = any(
        gfs_provider.is_current_or_future_valid_time(item["valid_time"])
        for item in result["valid_times"]
    )
    if auto_download and not has_realtime:
        download = gfs_downloader.start_realtime_download("no selectable realtime GFS files")
    return {**result, "domain_bbox": None, "source": "GFS GRIB2", "download": download}


def get_grid(cycle: str, forecast_hour: int, level: str, bbox, source: str | None = None):
    """Load a grid from the active provider."""
    mode = data_mode(source)
    if mode == "mock":
        return wind_provider.get_grid(cycle, forecast_hour, level, bbox)
    if mode == "wrf_cache":
        return wrf_cache_provider.get_grid(cycle, forecast_hour, level, bbox)
    return gfs_provider.get_grid(cycle, forecast_hour, level, bbox)


def get_grid_by_valid_time(valid_time: str, level: str, bbox, source: str | None = None):
    """Load a grid by forecast valid time from the active provider."""
    mode = data_mode(source)
    if mode == "mock":
        for cycle in wind_provider.CYCLES:
            for hour in wind_provider.FORECAST_HOURS:
                grid = wind_provider.get_grid(cycle, hour, level, bbox)
                if grid.valid_time == valid_time or grid.valid_time_bj == valid_time:
                    return grid
        raise ValueError(f"no mock grid for valid_time={valid_time}")
    if mode == "wrf_cache":
        return wrf_cache_provider.get_grid_by_valid_time(valid_time, level, bbox)
    return gfs_provider.get_grid_by_valid_time(valid_time, level, bbox)


def refresh(source: str | None = None) -> None:
    """Refresh the real-data file index after downloader updates."""
    mode = data_mode(source)
    if mode == "real":
        gfs_provider.refresh_file_index()
    elif mode == "wrf_cache":
        wrf_cache_provider.refresh_cache()


def maybe_start_gfs_download(source: str | None = None, reason: str = "missing GFS file") -> dict | None:
    """Start a realtime GFS download only when the selected source is raw GFS."""
    if data_mode(source) != "real":
        return None
    return gfs_downloader.start_realtime_download(reason)


def start_gfs_download(reason: str = "manual API request", force: bool = False) -> dict:
    """Expose manual realtime downloader startup to the API layer."""
    return gfs_downloader.start_realtime_download(reason, force=force)


def gfs_download_status() -> dict:
    return gfs_downloader.status()


def diagnostics(source: str | None = None) -> dict:
    """Return active data-provider diagnostics."""
    mode = data_mode(source)
    if mode == "mock":
        return {"mode": "mock"}
    if mode == "wrf_cache":
        return wrf_cache_provider.diagnostics()
    return {
        "mode": "real",
        "eccodes": gfs_provider.eccodes_runtime(),
        "download": gfs_downloader.status(),
    }
