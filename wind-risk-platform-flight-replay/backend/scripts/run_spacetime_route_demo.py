"""Spacetime A* demo for cross-time and cross-altitude route planning.

This is intentionally a standalone research/demo script. It reads platform
cache-like ``.npz`` files, where each file represents one forecast time and
contains:

    lons, lats, levels_m, u, v

Optional terrain/rain variables are used when present:

    hgt_surface / terrain / terrain_height / elevation
    apcp / prate / rain / rain_rate

The state is:

    (time_index, level_index, row, col)

Altitude is handled as:

    altitude_msl = hgt_surface[row, col] + level_agl

Safety uses AGL; smoothing uses adjacent MSL altitude changes and climb
gradient, so very large vertical jumps over short horizontal distances are
blocked.

Run a synthetic demo:

    python wind-risk-platform/backend/scripts/run_spacetime_route_demo.py --demo

Run on a platform cache directory:

    python wind-risk-platform/backend/scripts/run_spacetime_route_demo.py ^
      --data-source wrf ^
      --cache-dir data/wrf_platform_cache ^
      --cycle "2026-07-02 18:00 UTC" ^
      --forecast-hours 1,2,3,4,5,6 ^
      --levels 100,200,300 ^
      --start 118.5,30.75 ^
      --end 119.5,31.0

Run on existing GFS GRIB2 files:

    python wind-risk-platform/backend/scripts/run_spacetime_route_demo.py ^
      --data-source gfs ^
      --gfs-data-dirs data/gdex_gfs_0p25_global ^
      --cycle "2026-07-10 00:00 UTC" ^
      --forecast-hours 0,1,2,3 ^
      --levels 100,200,300 ^
      --start 118.5,30.75 ^
      --end 119.5,31.0
"""

from __future__ import annotations

import argparse
import os
import heapq
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.cost_evaluator import calculate_edge_cost, ensure_cost_config
from app.route_service import haversine_km
from app.wind_provider import WindGrid
from app import gfs_provider


State = tuple[int, int, int, int]  # time_index, level_index, row, col
NEIGHBOURS_8 = tuple((dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0))
TERRAIN_KEYS = ("hgt_surface", "terrain", "terrain_height", "elevation", "hgt_surface_m")
RAIN_KEYS = ("rain", "rain_rate", "apcp", "prate")
EPSILON = 1e-9


@dataclass
class SpacetimeConfig:
    cruise_speed_mps: float = 10.0
    vertical_speed_mps: float = 2.0
    max_wind_speed: float = 7.9
    min_agl_height_m: float = 60.0
    max_adjacent_msl_change_m: float = 100.0
    max_climb_gradient: float = 0.20
    altitude_change_weight: float = 3.0
    time_weight: float = 0.02
    max_iterations: int = 250000


@dataclass
class TimeSlice:
    valid_time: str
    forecast_hour: int | None
    lons: np.ndarray
    lats: np.ndarray
    levels_m: np.ndarray
    u: np.ndarray
    v: np.ndarray
    terrain: np.ndarray
    rain: np.ndarray | None = None
    source_path: str | None = None


def _as_text(value: Any) -> str:
    array = np.asarray(value)
    if array.shape == ():
        return str(array.item())
    return str(array)


def _parse_csv_numbers(text: str, cast=float) -> list:
    return [cast(item.strip()) for item in text.split(",") if item.strip()]


def _parse_bbox(text: str | None) -> tuple[float, float, float, float] | None:
    if not text:
        return None
    values = _parse_csv_numbers(text, float)
    if len(values) != 4:
        raise ValueError("--bbox must be min_lon,min_lat,max_lon,max_lat")
    min_lon, min_lat, max_lon, max_lat = values
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("--bbox must satisfy min_lon < max_lon and min_lat < max_lat")
    return min_lon, min_lat, max_lon, max_lat


def _route_bbox(
    start: tuple[float, float],
    end: tuple[float, float],
    buffer_deg: float,
) -> tuple[float, float, float, float]:
    min_lon = min(start[0], end[0]) - buffer_deg
    max_lon = max(start[0], end[0]) + buffer_deg
    min_lat = min(start[1], end[1]) - buffer_deg
    max_lat = max(start[1], end[1]) + buffer_deg
    return min_lon, min_lat, max_lon, max_lat


def _pick_array(data: np.lib.npyio.NpzFile, keys: tuple[str, ...]) -> np.ndarray | None:
    for key in keys:
        if key in data.files:
            return np.asarray(data[key], dtype=float)
    return None


def _rain_to_rate(values: np.ndarray | None) -> np.ndarray | None:
    if values is None:
        return None
    array = np.asarray(values, dtype=float)
    while array.ndim > 2:
        array = array[0]
    return array


def _load_npz_slice(path: Path) -> TimeSlice:
    with np.load(path, allow_pickle=False) as data:
        lons = np.asarray(data["lons"], dtype=float)
        lats = np.asarray(data["lats"], dtype=float)
        levels_m = np.asarray(data["levels_m"], dtype=float)
        u = np.asarray(data["u"], dtype=float)
        v = np.asarray(data["v"], dtype=float)
        valid_time = _as_text(data["valid_time_utc"]) if "valid_time_utc" in data.files else path.stem
        forecast_hour = int(np.asarray(data["forecast_hour"]).item()) if "forecast_hour" in data.files else None

        terrain = _pick_array(data, TERRAIN_KEYS)
        if terrain is None:
            print(f"[WARN] {path} lacks hgt_surface/terrain; using 0 m terrain.", file=sys.stderr)
            terrain = np.zeros((len(lats), len(lons)), dtype=float)
        while terrain.ndim > 2:
            terrain = terrain[0]

        rain = _rain_to_rate(_pick_array(data, RAIN_KEYS))

    if u.ndim != 3 or v.ndim != 3:
        raise ValueError(f"{path} must contain 3-D u/v arrays shaped (level, lat, lon)")
    if u.shape != v.shape:
        raise ValueError(f"{path} u/v shape mismatch: {u.shape} vs {v.shape}")
    if terrain.shape != u.shape[1:]:
        raise ValueError(f"{path} terrain shape {terrain.shape} does not match grid {u.shape[1:]}")
    if rain is not None and rain.shape != u.shape[1:]:
        raise ValueError(f"{path} rain shape {rain.shape} does not match grid {u.shape[1:]}")
    return TimeSlice(valid_time, forecast_hour, lons, lats, levels_m, u, v, terrain, rain, str(path))


def _load_index(cache_dir: Path) -> dict | None:
    path = cache_dir / "index.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _select_cache_files(args) -> list[Path]:
    cache_dir = args.cache_dir
    index = _load_index(cache_dir)
    if index is None:
        files = sorted(cache_dir.rglob("*.npz"))
        if not files:
            raise FileNotFoundError(f"No .npz files found under {cache_dir}")
        return files[: args.max_times]

    records = list(index.get("files", []))
    if args.cycle:
        records = [item for item in records if item.get("cycle") == args.cycle]
    if args.forecast_hours:
        hours = set(_parse_csv_numbers(args.forecast_hours, int))
        records = [item for item in records if int(item.get("forecast_hour", -999)) in hours]
    if args.valid_times:
        valid_times = set(item.strip() for item in args.valid_times.split(",") if item.strip())
        records = [item for item in records if item.get("valid_time") in valid_times or item.get("valid_time_bj") in valid_times]
    records = sorted(records, key=lambda item: (item.get("cycle", ""), int(item.get("forecast_hour", 0))))
    if not records:
        raise ValueError("No cache records match the requested cycle/forecast_hours/valid_times")
    return [cache_dir / item["path"] for item in records[: args.max_times]]


def _configure_gfs_data_dirs(args) -> None:
    if args.gfs_data_dirs:
        paths = [str(Path(item.strip()).expanduser().resolve()) for item in args.gfs_data_dirs.split(os.pathsep) if item.strip()]
        os.environ["GFS_DATA_DIRS"] = os.pathsep.join(paths)
        gfs_provider.refresh_file_index()


def _select_gfs_files(args) -> list[gfs_provider.GfsFile]:
    _configure_gfs_data_dirs(args)
    if args.valid_times:
        valid_times = [item.strip() for item in args.valid_times.split(",") if item.strip()]
        items = [gfs_provider.find_file_by_valid_time(value) for value in valid_times]
        return items[: args.max_times]

    files = list(gfs_provider.discover_files())
    if not files:
        raise FileNotFoundError(
            "No GFS GRIB2 files were discovered. Set --gfs-data-dirs or GFS_DATA_DIRS to an existing GFS directory."
        )

    cycle = args.cycle
    if not cycle:
        cycle = sorted({item.cycle for item in files})[-1]
        print(f"[GFS] --cycle not provided; using latest discovered cycle: {cycle}", file=sys.stderr)
    hours = _parse_csv_numbers(args.forecast_hours, int) if args.forecast_hours else sorted(
        {item.forecast_hour for item in files if item.cycle == cycle}
    )
    items = [gfs_provider.find_file(cycle, hour) for hour in hours]
    return items[: args.max_times]


def _normalize_gfs_surface_height(
    data_array,
    bbox: tuple[float, float, float, float] | None,
    reference_lons: np.ndarray,
    reference_lats: np.ndarray,
) -> np.ndarray:
    """Normalize GFS HGT/orography to the same lon/lat grid used by WindGrid."""
    terrain = gfs_provider._crop_data_array(data_array, bbox).load().squeeze(drop=True)
    lat_name = gfs_provider._coord_name(terrain, ("latitude", "lat"))
    lon_name = gfs_provider._coord_name(terrain, ("longitude", "lon"))
    values = np.asarray(terrain.transpose(lat_name, lon_name).values, dtype=float)
    lats = np.asarray(terrain[lat_name].values, dtype=float).reshape(-1)
    lons = np.asarray(terrain[lon_name].values, dtype=float).reshape(-1)

    lons = ((lons + 180.0) % 360.0) - 180.0
    lon_order = np.argsort(lons)
    lat_order = np.argsort(lats)[::-1]
    lons, lats = lons[lon_order], lats[lat_order]
    values = values[np.ix_(lat_order, lon_order)]

    unique_lons, unique_indices = np.unique(np.round(lons, 10), return_index=True)
    lons = unique_lons
    values = values[:, unique_indices]

    lon_indices = np.asarray([int(np.abs(lons - lon).argmin()) for lon in reference_lons])
    lat_indices = np.asarray([int(np.abs(lats - lat).argmin()) for lat in reference_lats])
    if not np.allclose(lons[lon_indices], reference_lons, atol=1e-6) or not np.allclose(lats[lat_indices], reference_lats, atol=1e-6):
        raise ValueError("GFS terrain HGT grid does not align with normalized wind grid")
    return values[np.ix_(lat_indices, lon_indices)]


def _load_gfs_terrain(
    item: gfs_provider.GfsFile,
    bbox: tuple[float, float, float, float] | None,
    reference_lons: np.ndarray,
    reference_lats: np.ndarray,
) -> np.ndarray:
    terrain = gfs_provider._open_surface_height(item.path)
    return _normalize_gfs_surface_height(terrain, bbox, reference_lons, reference_lats)


def _load_gfs_slices(
    args,
    start: tuple[float, float],
    end: tuple[float, float],
) -> list[TimeSlice]:
    requested_levels = _parse_csv_numbers(args.levels, float) if args.levels else [100.0, 200.0, 300.0]
    bbox = _parse_bbox(args.bbox)
    if bbox is None:
        bbox = _route_bbox(start, end, args.route_buffer_deg)
        print(f"[GFS] auto bbox={','.join(f'{value:.3f}' for value in bbox)}", file=sys.stderr)

    slices: list[TimeSlice] = []
    for item in _select_gfs_files(args):
        grids = [
            gfs_provider.get_grid(item.cycle, item.forecast_hour, f"{int(round(level))}m AGL", bbox)
            for level in requested_levels
        ]
        reference = grids[0]
        for grid in grids[1:]:
            if not np.allclose(grid.lons, reference.lons) or not np.allclose(grid.lats, reference.lats):
                raise ValueError(f"GFS grids for {item.valid_time} do not share the same lon/lat grid")

        terrain = _load_gfs_terrain(item, bbox, reference.lons, reference.lats)
        slices.append(
            TimeSlice(
                valid_time=item.valid_time,
                forecast_hour=item.forecast_hour,
                lons=reference.lons,
                lats=reference.lats,
                levels_m=np.asarray(requested_levels, dtype=float),
                u=np.stack([grid.u for grid in grids]),
                v=np.stack([grid.v for grid in grids]),
                terrain=terrain,
                rain=None,
                source_path=str(item.path),
            )
        )
    return slices


def _synthetic_slices() -> list[TimeSlice]:
    lons = np.linspace(118.0, 119.0, 11)
    lats = np.linspace(31.0, 30.0, 11)
    levels = np.asarray([80.0, 150.0, 250.0], dtype=float)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    terrain = 40 + 120 * np.exp(-((lon_grid - 118.55) ** 2 + (lat_grid - 30.55) ** 2) / 0.03)
    slices: list[TimeSlice] = []
    for time_index in range(5):
        u = np.zeros((len(levels), len(lats), len(lons)), dtype=float)
        v = np.zeros_like(u)
        for level_index, level in enumerate(levels):
            base = 1.5 + 0.003 * level + 0.2 * time_index
            u[level_index] = base + 2.8 * np.exp(-((lon_grid - (118.35 + 0.08 * time_index)) ** 2 + (lat_grid - 30.65) ** 2) / 0.015)
            v[level_index] = 0.6 * np.sin((lon_grid - 118.4) * 5)
            # Higher layer has a useful eastward wind corridor after +2h.
            if level >= 150 and time_index >= 2:
                u[level_index, 6:9, :] -= 1.2
        slices.append(
            TimeSlice(
                valid_time=f"demo +{time_index}h",
                forecast_hour=time_index,
                lons=lons,
                lats=lats,
                levels_m=levels,
                u=u,
                v=v,
                terrain=terrain,
                rain=None,
                source_path="synthetic",
            )
        )
    return slices


def _subset_levels(slice_: TimeSlice, requested_levels: list[float] | None) -> TimeSlice:
    if not requested_levels:
        return slice_
    indices = []
    for level in requested_levels:
        matches = np.where(np.isclose(slice_.levels_m, level, atol=0.1))[0]
        if len(matches) == 0:
            raise ValueError(f"Cache does not contain requested level {level} m AGL; available={slice_.levels_m.tolist()}")
        indices.append(int(matches[0]))
    return TimeSlice(
        valid_time=slice_.valid_time,
        forecast_hour=slice_.forecast_hour,
        lons=slice_.lons,
        lats=slice_.lats,
        levels_m=slice_.levels_m[indices],
        u=slice_.u[indices],
        v=slice_.v[indices],
        terrain=slice_.terrain,
        rain=slice_.rain,
        source_path=slice_.source_path,
    )


def load_slices(args, start: tuple[float, float], end: tuple[float, float]) -> list[TimeSlice]:
    if args.demo:
        slices = _synthetic_slices()
    elif args.data_source == "gfs":
        slices = _load_gfs_slices(args, start, end)
    else:
        slices = [_load_npz_slice(path) for path in _select_cache_files(args)]

    requested_levels = _parse_csv_numbers(args.levels, float) if args.levels else None
    if args.demo or args.data_source != "gfs":
        slices = [_subset_levels(item, requested_levels) for item in slices]
    reference = slices[0]
    for item in slices[1:]:
        if not np.allclose(item.lons, reference.lons) or not np.allclose(item.lats, reference.lats):
            raise ValueError("All time slices must use the same lon/lat grid")
        if not np.allclose(item.levels_m, reference.levels_m):
            raise ValueError("All time slices must use the same selected levels")
    return slices


class SpacetimeAStar:
    def __init__(self, slices: list[TimeSlice], start: tuple[float, float], end: tuple[float, float], config: SpacetimeConfig):
        self.slices = slices
        self.config = config
        self.lons = slices[0].lons
        self.lats = slices[0].lats
        self.levels_m = slices[0].levels_m
        self.start_node = self.nearest_node(start)
        self.goal_node = self.nearest_node(end)
        self.cost_config = ensure_cost_config(
            {
                "weights": {
                    "alpha_distance": 1.0,
                    "beta_wind": 12.0,
                    "gamma_headwind": 0.7,
                    "delta_crosswind": 0.2,
                    "eta_terrain": 5.0,
                    "mu_rain": 8.0,
                },
                "thresholds": {
                    "max_wind_speed": config.max_wind_speed,
                    "min_agl_height": config.min_agl_height_m,
                },
            }
        )

    def nearest_node(self, point: tuple[float, float]) -> tuple[int, int]:
        lon, lat = point
        return int(np.abs(self.lats - lat).argmin()), int(np.abs(self.lons - lon).argmin())

    def in_bounds(self, row: int, col: int) -> bool:
        return 0 <= row < len(self.lats) and 0 <= col < len(self.lons)

    def cell_point(self, row: int, col: int) -> tuple[float, float]:
        return float(self.lons[col]), float(self.lats[row])

    def terrain(self, time_index: int, row: int, col: int) -> float:
        return float(self.slices[time_index].terrain[row, col])

    def altitude_msl(self, state: State) -> float:
        time_index, level_index, row, col = state
        return self.terrain(time_index, row, col) + float(self.levels_m[level_index])

    def elapsed_to_time_index(self, elapsed_sec: float) -> int:
        # Cache products are usually hourly. This keeps the prototype simple
        # while still letting the path use future forecast slices as ETA grows.
        return min(len(self.slices) - 1, max(0, int(round(elapsed_sec / 3600.0))))

    def wind_grid(self, time_index: int, level_index: int) -> WindGrid:
        slice_ = self.slices[time_index]
        return WindGrid(
            lons=slice_.lons,
            lats=slice_.lats,
            u=slice_.u[level_index],
            v=slice_.v[level_index],
            cycle="spacetime",
            forecast_hour=slice_.forecast_hour or time_index,
            level=f"{int(round(self.levels_m[level_index]))}m AGL",
            valid_time=slice_.valid_time,
            source=slice_.source_path or "spacetime npz",
        )

    def horizontal_distance_m(self, a: tuple[int, int], b: tuple[int, int]) -> float:
        return 1000.0 * haversine_km(self.cell_point(*a), self.cell_point(*b))

    def is_goal(self, state: State) -> bool:
        return (state[2], state[3]) == self.goal_node

    def heuristic(self, state: State) -> float:
        _, _, row, col = state
        return haversine_km(self.cell_point(row, col), self.cell_point(*self.goal_node))

    def altitude_transition_ok(self, current: State, candidate: State, horizontal_m: float) -> tuple[bool, str | None, float]:
        dz = self.altitude_msl(candidate) - self.altitude_msl(current)
        abs_dz = abs(dz)
        if abs_dz > self.config.max_adjacent_msl_change_m:
            return False, "adjacent MSL altitude jump exceeds limit", dz
        if horizontal_m > 1.0:
            gradient = abs_dz / horizontal_m
            if gradient > self.config.max_climb_gradient:
                return False, "climb/descent gradient exceeds limit", dz
        return True, None, dz

    def edge_cost(self, current: State, candidate_level: int, row_to: int, col_to: int, elapsed_sec: float) -> tuple[float, State | None, float, str | None]:
        time_from, level_from, row_from, col_from = current
        horizontal_m = self.horizontal_distance_m((row_from, col_from), (row_to, col_to))
        if horizontal_m <= EPSILON:
            return float("inf"), None, elapsed_sec, "zero horizontal move"

        travel_sec = horizontal_m / max(self.config.cruise_speed_mps, EPSILON)
        rough_time_to = self.elapsed_to_time_index(elapsed_sec + travel_sec)
        candidate = (rough_time_to, candidate_level, row_to, col_to)
        ok, reason, dz_msl = self.altitude_transition_ok(current, candidate, horizontal_m)
        if not ok:
            return float("inf"), None, elapsed_sec, reason

        vertical_sec = abs(dz_msl) / max(self.config.vertical_speed_mps, EPSILON)
        next_elapsed = elapsed_sec + travel_sec + vertical_sec
        time_to = self.elapsed_to_time_index(next_elapsed)
        candidate = (time_to, candidate_level, row_to, col_to)
        ok, reason, dz_msl = self.altitude_transition_ok(current, candidate, horizontal_m)
        if not ok:
            return float("inf"), None, elapsed_sec, reason

        if self.levels_m[candidate_level] < self.config.min_agl_height_m:
            return float("inf"), None, elapsed_sec, "AGL lower than minimum safety height"

        grid = self.wind_grid(time_to, candidate_level)
        node_from = {"row": row_from, "col": col_from, "altitude_msl": self.altitude_msl(current)}
        node_to = {"row": row_to, "col": col_to, "altitude_msl": self.altitude_msl(candidate)}
        terrain_data = {"hgt_surface": self.slices[time_to].terrain}
        rain_data = None if self.slices[time_to].rain is None else {"rain": self.slices[time_to].rain}
        edge = calculate_edge_cost(node_from, node_to, grid, terrain_data, rain_data, self.cost_config)
        if edge.blocked or not np.isfinite(edge.total_cost):
            return float("inf"), None, elapsed_sec, edge.reason or "edge blocked"

        altitude_cost = self.config.altitude_change_weight * abs(dz_msl) / max(self.config.max_adjacent_msl_change_m, 1.0)
        time_cost = self.config.time_weight * (travel_sec + vertical_sec) / 60.0
        return edge.total_cost + altitude_cost + time_cost, candidate, next_elapsed, None

    def neighbours(self, state: State, elapsed_sec: float):
        _, level_index, row, col = state
        level_candidates = [
            index
            for index, level in enumerate(self.levels_m)
            if abs(float(level) - float(self.levels_m[level_index])) <= self.config.max_adjacent_msl_change_m + EPSILON
        ]
        for dr, dc in NEIGHBOURS_8:
            row_to, col_to = row + dr, col + dc
            if not self.in_bounds(row_to, col_to):
                continue
            for next_level in level_candidates:
                yield next_level, row_to, col_to

    def reconstruct(self, parents: dict[State, State | None], goal: State, elapsed: dict[State, float], cost: float) -> dict[str, Any]:
        states = []
        current: State | None = goal
        while current is not None:
            states.append(current)
            current = parents.get(current)
        states.reverse()

        points = []
        max_wind = 0.0
        max_rain = None
        total_distance = 0.0
        for index, state in enumerate(states):
            time_index, level_index, row, col = state
            slice_ = self.slices[time_index]
            wind_speed = float(math.hypot(slice_.u[level_index, row, col], slice_.v[level_index, row, col]))
            max_wind = max(max_wind, wind_speed)
            rain_value = None if slice_.rain is None else float(slice_.rain[row, col])
            if rain_value is not None:
                max_rain = rain_value if max_rain is None else max(max_rain, rain_value)
            if index > 0:
                previous = states[index - 1]
                total_distance += self.horizontal_distance_m((previous[2], previous[3]), (row, col)) / 1000.0
            points.append(
                {
                    "lon": round(float(self.lons[col]), 6),
                    "lat": round(float(self.lats[row]), 6),
                    "valid_time": slice_.valid_time,
                    "eta_min": round(elapsed[state] / 60.0, 2),
                    "level_agl_m": round(float(self.levels_m[level_index]), 2),
                    "terrain_height_m": round(self.terrain(time_index, row, col), 2),
                    "altitude_msl_m": round(self.altitude_msl(state), 2),
                    "wind_speed_mps": round(wind_speed, 3),
                    "u_mps": round(float(slice_.u[level_index, row, col]), 3),
                    "v_mps": round(float(slice_.v[level_index, row, col]), 3),
                    "rain": None if rain_value is None else round(rain_value, 3),
                }
            )
        return {
            "path": points,
            "summary": {
                "total_cost": round(cost, 3),
                "total_distance_km": round(total_distance, 3),
                "total_time_min": round(elapsed[goal] / 60.0, 2),
                "max_wind_speed_mps": round(max_wind, 3),
                "max_rain": None if max_rain is None else round(max_rain, 3),
                "height_changes": sum(1 for a, b in zip(states, states[1:]) if a[1] != b[1]),
                "expanded_nodes": None,
            },
        }

    def plan(self) -> dict[str, Any]:
        level_indices = [index for index, level in enumerate(self.levels_m) if level >= self.config.min_agl_height_m]
        if not level_indices:
            raise ValueError("No selected level satisfies min_agl_height_m")

        start_level = level_indices[0]
        start: State = (0, start_level, self.start_node[0], self.start_node[1])
        queue: list[tuple[float, float, int, State]] = []
        counter = 0
        best: dict[State, float] = {start: 0.0}
        elapsed: dict[State, float] = {start: 0.0}
        parents: dict[State, State | None] = {start: None}
        heapq.heappush(queue, (self.heuristic(start), 0.0, counter, start))
        expanded = 0

        while queue:
            _, cost_so_far, _, state = heapq.heappop(queue)
            if cost_so_far != best.get(state):
                continue
            if self.is_goal(state):
                result = self.reconstruct(parents, state, elapsed, cost_so_far)
                result["summary"]["expanded_nodes"] = expanded
                return result
            expanded += 1
            if expanded > self.config.max_iterations:
                break

            for level_to, row_to, col_to in self.neighbours(state, elapsed[state]):
                step_cost, candidate, next_elapsed, _reason = self.edge_cost(state, level_to, row_to, col_to, elapsed[state])
                if candidate is None or not np.isfinite(step_cost):
                    continue
                next_cost = cost_so_far + step_cost
                if next_cost < best.get(candidate, float("inf")):
                    best[candidate] = next_cost
                    elapsed[candidate] = next_elapsed
                    parents[candidate] = state
                    counter += 1
                    heapq.heappush(queue, (next_cost + self.heuristic(candidate), next_cost, counter, candidate))

        raise ValueError(f"No spacetime route found after expanding {expanded} nodes")


def parse_args():
    parser = argparse.ArgumentParser(description="Cross-time/cross-altitude Spacetime A* route planning demo.")
    parser.add_argument("--demo", action="store_true", help="Run on a synthetic in-memory dataset.")
    parser.add_argument("--data-source", choices=("wrf", "gfs"), default="wrf", help="Use WRF platform npz cache or GFS GRIB2 files.")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/wrf_platform_cache"))
    parser.add_argument(
        "--gfs-data-dirs",
        default=None,
        help="GFS GRIB2 search roots separated by the OS path separator; defaults to GFS_DATA_DIRS/provider defaults.",
    )
    parser.add_argument("--cycle", default=None, help='Cycle label, e.g. "2026-07-02 18:00 UTC".')
    parser.add_argument("--forecast-hours", default="1,2,3,4,5,6", help="Comma-separated forecast hours.")
    parser.add_argument("--valid-times", default=None, help="Comma-separated UTC/BJ valid-time labels.")
    parser.add_argument("--max-times", type=int, default=7)
    parser.add_argument("--levels", default="100,200,300", help="Comma-separated AGL levels in meters.")
    parser.add_argument("--start", default="118.0,30.5", help="lon,lat")
    parser.add_argument("--end", default="119.0,30.5", help="lon,lat")
    parser.add_argument("--bbox", default=None, help="Optional data bbox: min_lon,min_lat,max_lon,max_lat.")
    parser.add_argument(
        "--route-buffer-deg",
        type=float,
        default=0.75,
        help="Auto bbox buffer around start/end when --data-source gfs and --bbox is omitted.",
    )
    parser.add_argument("--cruise-speed-mps", type=float, default=10.0)
    parser.add_argument("--vertical-speed-mps", type=float, default=2.0)
    parser.add_argument("--max-wind-speed", type=float, default=7.9)
    parser.add_argument("--min-agl-height-m", type=float, default=60.0)
    parser.add_argument("--max-adjacent-msl-change-m", type=float, default=100.0)
    parser.add_argument("--max-climb-gradient", type=float, default=0.20, help="max |dz_msl| / horizontal_distance_m")
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    start = tuple(_parse_csv_numbers(args.start, float))
    end = tuple(_parse_csv_numbers(args.end, float))
    if len(start) != 2 or len(end) != 2:
        raise ValueError("--start and --end must be lon,lat")

    config = SpacetimeConfig(
        cruise_speed_mps=args.cruise_speed_mps,
        vertical_speed_mps=args.vertical_speed_mps,
        max_wind_speed=args.max_wind_speed,
        min_agl_height_m=args.min_agl_height_m,
        max_adjacent_msl_change_m=args.max_adjacent_msl_change_m,
        max_climb_gradient=args.max_climb_gradient,
    )
    slices = load_slices(args, start, end)

    print(f"[DATA] source={'synthetic' if args.demo else args.data_source} time_slices={len(slices)} levels={slices[0].levels_m.tolist()}")
    print(f"[DATA] first_valid_time={slices[0].valid_time} terrain={'yes' if np.any(slices[0].terrain) else 'missing/zero'}")
    planner = SpacetimeAStar(slices, start, end, config)
    t0 = time.perf_counter()
    result = planner.plan()
    result["summary"]["planning_time_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    result["config"] = {
        "data_source": "synthetic" if args.demo else args.data_source,
        "cruise_speed_mps": config.cruise_speed_mps,
        "vertical_speed_mps": config.vertical_speed_mps,
        "max_wind_speed": config.max_wind_speed,
        "min_agl_height_m": config.min_agl_height_m,
        "max_adjacent_msl_change_m": config.max_adjacent_msl_change_m,
        "max_climb_gradient": config.max_climb_gradient,
    }

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text, encoding="utf-8")
        print(f"[OUT] {args.output_json}")
    else:
        print(text)


if __name__ == "__main__":
    main()
