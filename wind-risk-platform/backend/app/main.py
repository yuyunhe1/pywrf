"""FastAPI entrypoint for the GFS wind-risk service."""

import numpy as np

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from .data_provider import availability, diagnostics, get_grid, get_grid_by_valid_time, refresh
from .models import RouteAnalyzeRequest
from .route_service import analyze_route, sample_route
from .wind_provider import parse_bbox, point_value

app = FastAPI(title="UAV Low-altitude Wind Risk API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def load_grid(
    cycle: str | None,
    forecast_hour: int | None,
    level: str,
    bbox: str | None = None,
    valid_time: str | None = None,
    source: str | None = None,
):
    """Validate common query parameters and load a cached regular grid."""
    try:
        parsed_bbox = parse_bbox(bbox)
        if valid_time:
            return get_grid_by_valid_time(valid_time, level, parsed_bbox, source)
        if cycle is None or forecast_hour is None:
            raise ValueError("cycle and forecast_hour are required when valid_time is not provided")
        return get_grid(cycle, forecast_hour, level, parsed_bbox, source)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def metadata(grid) -> dict:
    """Build common wind-field metadata."""
    return {
        "source": grid.source,
        "cycle": grid.cycle,
        "cycle_bj": getattr(grid, "cycle_bj", None),
        "forecast_hour": grid.forecast_hour,
        "valid_time": grid.valid_time,
        "valid_time_bj": getattr(grid, "valid_time_bj", None),
        "level": grid.level,
        "unit": "m/s",
        "bbox": [float(grid.lons[0]), float(grid.lats[-1]), float(grid.lons[-1]), float(grid.lats[0])],
        "grid": {
            "nx": len(grid.lons),
            "ny": len(grid.lats),
            "dx": round(float(grid.lons[1] - grid.lons[0]), 8),
            "dy": round(float(grid.lats[0] - grid.lats[1]), 8),
        },
    }


@app.get("/", include_in_schema=False)
def frontend():
    """Redirect the backend root URL to the local Vue development UI."""
    return RedirectResponse("http://127.0.0.1:5173")


@app.get("/api/health")
def health(source: str | None = None):
    """Return service health."""
    try:
        return {"status": "ok", **diagnostics(source)}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/times")
def times(refresh_files: bool = Query(default=False, alias="refresh"), source: str | None = None):
    """Return cycles, forecast hours, and AGL levels discovered from existing GFS files."""
    if refresh_files:
        refresh(source)
    return availability(source)


@app.get("/api/wind")
def wind(
    cycle: str | None = None,
    forecast_hour: int | None = Query(default=None, ge=0),
    level: str = "100m AGL",
    bbox: str | None = None,
    valid_time: str | None = None,
    source: str | None = None,
):
    """Return regular-grid U/V arrays in leaflet-velocity JSON format."""
    grid = load_grid(cycle, forecast_hour, level, bbox, valid_time, source)
    dx = float(grid.lons[1] - grid.lons[0])
    dy = float(grid.lats[0] - grid.lats[1])
    common = {
        "parameterUnit": "m.s-1",
        "parameterCategory": 2,
        "nx": len(grid.lons),
        "ny": len(grid.lats),
        "lo1": float(grid.lons[0]),
        "la1": float(grid.lats[0]),
        "lo2": float(grid.lons[-1]),
        "la2": float(grid.lats[-1]),
        "dx": dx,
        "dy": dy,
        "refTime": grid.valid_time,
    }
    velocity = [
        {"header": {**common, "parameterNumber": 2, "parameterNumberName": "eastward_wind"}, "data": grid.u.ravel().round(3).tolist()},
        {"header": {**common, "parameterNumber": 3, "parameterNumberName": "northward_wind"}, "data": grid.v.ravel().round(3).tolist()},
    ]
    return {"metadata": metadata(grid), "velocity": velocity}


@app.get("/api/heatmap")
def heatmap(
    cycle: str | None = None,
    forecast_hour: int | None = Query(default=None, ge=0),
    level: str = "100m AGL",
    bbox: str | None = None,
    valid_time: str | None = None,
    source: str | None = None,
):
    """Return a regular wind-speed matrix for a Leaflet canvas raster layer."""
    grid = load_grid(cycle, forecast_hour, level, bbox, valid_time, source)
    speed = (grid.u**2 + grid.v**2) ** 0.5
    finite_speed = speed[np.isfinite(speed)]
    return {
        "metadata": metadata(grid),
        "wind_speed": {
            "header": {
                "nx": len(grid.lons),
                "ny": len(grid.lats),
                "lo1": float(grid.lons[0]),
                "la1": float(grid.lats[0]),
                "lo2": float(grid.lons[-1]),
                "la2": float(grid.lats[-1]),
                "dx": float(grid.lons[1] - grid.lons[0]),
                "dy": float(grid.lats[0] - grid.lats[1]),
                "unit": "m/s",
            },
            "data": speed.ravel().round(3).tolist(),
            "min": round(float(finite_speed.min()), 3),
            "max": round(float(finite_speed.max()), 3),
        },
    }


@app.get("/api/point")
def point(
    lon: float,
    lat: float,
    cycle: str | None = None,
    forecast_hour: int | None = Query(default=None, ge=0),
    level: str = "100m AGL",
    valid_time: str | None = None,
    source: str | None = None,
):
    """Query the nearest wind grid point. Inputs use longitude then latitude."""
    point_bbox = f"{lon - 0.5},{lat - 0.5},{lon + 0.5},{lat + 0.5}"
    grid = load_grid(cycle, forecast_hour, level, point_bbox, valid_time, source)
    domain = metadata(grid)["bbox"]
    if not (domain[0] <= lon <= domain[2] and domain[1] <= lat <= domain[3]):
        raise HTTPException(status_code=400, detail="point is outside the available wind domain")
    return {**point_value(grid, lon, lat), "level": grid.level, "valid_time": grid.valid_time, "unit": "m/s"}


@app.post("/api/route/analyze")
def route_analyze(request: RouteAnalyzeRequest):
    """Sample a [lon, lat] route and return wind-risk statistics."""
    lons = [point[0] for point in request.points]
    lats = [point[1] for point in request.points]
    route_bbox = f"{min(lons) - 0.5},{min(lats) - 0.5},{max(lons) + 0.5},{max(lats) + 0.5}"
    grid = load_grid(request.cycle, request.forecast_hour, request.level, route_bbox, request.valid_time, request.source)
    samples = sample_route(request.points, grid, request.thresholds, request.sample_interval_km)
    return {**analyze_route(samples, request.thresholds), "metadata": metadata(grid)}
