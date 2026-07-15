"""Multi-altitude wind-aware route planning.

This module brings the cross-height part of the standalone spacetime demo into
the platform API.  A state is ``(level_index, row, col)``.  The planner can move
through the 8 horizontal neighbours and may switch to a nearby AGL level during
that horizontal move, while enforcing altitude smoothness in AMSL space:

    altitude_amsl = terrain_height + altitude_agl

The hard constraints intentionally avoid pure vertical jumps at the same grid
cell; every height change must happen while the aircraft advances horizontally.
"""

from __future__ import annotations

import heapq
import math
import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from .cost_evaluator import calculate_edge_cost, cost_config_from_thresholds
from .models import Thresholds
from .route_service import haversine_km, risk_name
from .wind_provider import WindGrid


NEIGHBOURS_8 = tuple((dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0))
EPSILON = 1e-9


@dataclass(frozen=True)
class MultiAltitudeConfig:
    cruise_speed_mps: float = 10.0
    vertical_speed_mps: float = 2.0
    min_agl_height_m: float = 60.0
    max_adjacent_msl_change_m: float = 100.0
    max_climb_gradient: float = 0.12
    altitude_change_weight: float = 3.0
    max_iterations: int = 250000


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def config_from_env() -> MultiAltitudeConfig:
    return MultiAltitudeConfig(
        cruise_speed_mps=_float_env("ROUTE_PLAN_CRUISE_SPEED_MPS", 10.0),
        vertical_speed_mps=_float_env("ROUTE_PLAN_VERTICAL_SPEED_MPS", 2.0),
        min_agl_height_m=_float_env("ROUTE_PLAN_MIN_AGL_M", 60.0),
        max_adjacent_msl_change_m=_float_env("ROUTE_PLAN_MAX_ADJACENT_MSL_CHANGE_M", 100.0),
        max_climb_gradient=_float_env("ROUTE_PLAN_MAX_CLIMB_GRADIENT", 0.12),
        altitude_change_weight=_float_env("ROUTE_PLAN_ALTITUDE_CHANGE_WEIGHT", 3.0),
        max_iterations=_int_env("ROUTE_PLAN_MAX_ALTITUDE_ITERATIONS", 250000),
    )


def level_height_m(level: str) -> float | None:
    text = level.lower().replace("agl", "").replace(" ", "")
    number = ""
    for char in text:
        if char.isdigit() or char == ".":
            number += char
        elif number:
            break
    return float(number) if number else None


def _nearest_index(values: np.ndarray, value: float) -> int:
    return int(np.abs(values - value).argmin())


def _cell_point(grid: WindGrid, row: int, col: int) -> tuple[float, float]:
    return float(grid.lons[col]), float(grid.lats[row])


def _terrain_at(grid: WindGrid, row: int, col: int) -> float:
    terrain = getattr(grid, "terrain", None)
    if terrain is None:
        return 0.0
    value = float(np.asarray(terrain)[row, col])
    return value if np.isfinite(value) else 0.0


def _same_grid(grids: list[WindGrid]) -> bool:
    reference = grids[0]
    return all(np.allclose(grid.lons, reference.lons) and np.allclose(grid.lats, reference.lats) for grid in grids[1:])


def _bearing_deg(start: tuple[float, float], end: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (start[0], start[1], end[0], end[1]))
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _turn_cost(previous: tuple[int, int] | None, current: tuple[int, int]) -> float:
    if previous is None:
        return 0.0
    dot = previous[0] * current[0] + previous[1] * current[1]
    denominator = math.hypot(*previous) * math.hypot(*current)
    if denominator <= EPSILON:
        return 0.0
    angle = math.degrees(math.acos(float(np.clip(dot / denominator, -1, 1))))
    return max(0.0, angle - 25.0) / 20.0


class MultiAltitudeAStar:
    def __init__(
        self,
        grids: list[WindGrid],
        start: tuple[float, float],
        end: tuple[float, float],
        thresholds: Thresholds,
        cost_config: Any | None = None,
        config: MultiAltitudeConfig | None = None,
    ):
        if len(grids) < 2:
            raise ValueError("multi-altitude planning requires at least two height layers")
        if not _same_grid(grids):
            raise ValueError("all height layers must share the same lon/lat grid")
        parsed_levels = [level_height_m(grid.level) for grid in grids]
        if any(value is None for value in parsed_levels):
            raise ValueError("multi-altitude planning only supports numeric AGL layers")
        order = np.argsort(np.asarray(parsed_levels, dtype=float))
        self.grids = [grids[int(index)] for index in order]
        self.levels_m = np.asarray([parsed_levels[int(index)] for index in order], dtype=float)
        self.start = start
        self.end = end
        self.thresholds = thresholds
        self.config = config or config_from_env()
        merged_cost_config = dict(cost_config or {}) if isinstance(cost_config, dict) else {}
        threshold_overrides = dict(merged_cost_config.get("thresholds", {}))
        threshold_overrides["min_agl_height"] = self.config.min_agl_height_m
        if isinstance(cost_config, dict):
            merged_cost_config["thresholds"] = threshold_overrides
            self.cost_config = cost_config_from_thresholds(thresholds, merged_cost_config)
        else:
            self.cost_config = cost_config_from_thresholds(thresholds, cost_config)
        self.reference = self.grids[0]
        self.start_node = (_nearest_index(self.reference.lats, start[1]), _nearest_index(self.reference.lons, start[0]))
        self.end_node = (_nearest_index(self.reference.lats, end[1]), _nearest_index(self.reference.lons, end[0]))

    def terrain(self, level_index: int, row: int, col: int) -> float:
        return _terrain_at(self.grids[level_index], row, col)

    def altitude_msl(self, level_index: int, row: int, col: int) -> float:
        return self.terrain(level_index, row, col) + float(self.levels_m[level_index])

    def horizontal_distance_m(self, a: tuple[int, int], b: tuple[int, int]) -> float:
        return 1000.0 * haversine_km(_cell_point(self.reference, *a), _cell_point(self.reference, *b))

    def heuristic(self, row: int, col: int) -> float:
        return haversine_km(_cell_point(self.reference, row, col), _cell_point(self.reference, *self.end_node))

    def in_bounds(self, row: int, col: int) -> bool:
        return 0 <= row < len(self.reference.lats) and 0 <= col < len(self.reference.lons)

    def level_candidates(self, current_level: int) -> list[int]:
        current_agl = self.levels_m[current_level]
        return [
            index
            for index, level in enumerate(self.levels_m)
            if abs(float(level) - float(current_agl)) <= self.config.max_adjacent_msl_change_m + EPSILON
            and float(level) >= self.config.min_agl_height_m
        ]

    def altitude_transition_ok(
        self,
        level_from: int,
        row_from: int,
        col_from: int,
        level_to: int,
        row_to: int,
        col_to: int,
        horizontal_m: float,
    ) -> tuple[bool, float]:
        dz = self.altitude_msl(level_to, row_to, col_to) - self.altitude_msl(level_from, row_from, col_from)
        abs_dz = abs(dz)
        if abs_dz > self.config.max_adjacent_msl_change_m:
            return False, dz
        if horizontal_m > 1.0 and abs_dz / horizontal_m > self.config.max_climb_gradient:
            return False, dz
        return True, dz

    def edge_cost(
        self,
        level_from: int,
        row_from: int,
        col_from: int,
        level_to: int,
        row_to: int,
        col_to: int,
    ) -> float:
        horizontal_m = self.horizontal_distance_m((row_from, col_from), (row_to, col_to))
        if horizontal_m <= EPSILON:
            return float("inf")
        ok, dz_msl = self.altitude_transition_ok(level_from, row_from, col_from, level_to, row_to, col_to, horizontal_m)
        if not ok:
            return float("inf")
        grid = self.grids[level_to]
        node_from = {
            "row": row_from,
            "col": col_from,
            "altitude_msl": self.altitude_msl(level_from, row_from, col_from),
            "agl_height": float(self.levels_m[level_from]),
        }
        node_to = {
            "row": row_to,
            "col": col_to,
            "altitude_msl": self.altitude_msl(level_to, row_to, col_to),
            "agl_height": float(self.levels_m[level_to]),
        }
        terrain_data = {"hgt_surface": grid.terrain} if getattr(grid, "terrain", None) is not None else None
        edge = calculate_edge_cost(node_from, node_to, grid, terrain_data, None, self.cost_config)
        if edge.blocked or not np.isfinite(edge.total_cost):
            return float("inf")
        vertical_sec = abs(dz_msl) / max(self.config.vertical_speed_mps, EPSILON)
        travel_sec = horizontal_m / max(self.config.cruise_speed_mps, EPSILON)
        altitude_cost = self.config.altitude_change_weight * abs(dz_msl) / max(self.config.max_adjacent_msl_change_m, 1.0)
        time_cost = 0.01 * (travel_sec + vertical_sec) / 60.0
        return float(edge.total_cost + altitude_cost + time_cost)

    def initial_states(self) -> list[tuple[int, int, int, tuple[int, int] | None]]:
        row, col = self.start_node
        states = []
        for level_index, level in enumerate(self.levels_m):
            if level < self.config.min_agl_height_m:
                continue
            grid = self.grids[level_index]
            node = {
                "row": row,
                "col": col,
                "altitude_msl": self.altitude_msl(level_index, row, col),
                "agl_height": float(level),
            }
            terrain_data = {"hgt_surface": grid.terrain} if getattr(grid, "terrain", None) is not None else None
            edge = calculate_edge_cost(node, node, grid, terrain_data, None, self.cost_config)
            if not edge.blocked:
                states.append((level_index, row, col, None))
        if not states:
            raise ValueError("起点在所有候选高度层均不可飞")
        return states

    def reconstruct(
        self,
        parents: dict[tuple[int, int, int, tuple[int, int] | None], tuple[int, int, int, tuple[int, int] | None] | None],
        goal_state: tuple[int, int, int, tuple[int, int] | None],
        best: dict[tuple[int, int, int, tuple[int, int] | None], float],
        expanded_nodes: int,
    ) -> dict:
        states = []
        state: tuple[int, int, int, tuple[int, int] | None] | None = goal_state
        while state is not None:
            states.append(state)
            state = parents.get(state)
        states.reverse()

        simplified = [states[0]]
        for index in range(1, len(states) - 1):
            a, b, c = simplified[-1], states[index], states[index + 1]
            dir_ab = (b[1] - a[1], b[2] - a[2])
            dir_bc = (c[1] - b[1], c[2] - b[2])
            level_changed = a[0] != b[0] or b[0] != c[0]
            if level_changed or dir_ab != dir_bc:
                simplified.append(b)
        simplified.append(states[-1])

        def waypoint_from_state(state_item, lon_lat_override: tuple[float, float] | None = None) -> dict:
            level_index, row, col, _ = state_item
            grid = self.grids[level_index]
            lon, lat = lon_lat_override or _cell_point(grid, row, col)
            agl = float(self.levels_m[level_index])
            terrain = self.terrain(level_index, row, col)
            u = float(grid.u[row, col])
            v = float(grid.v[row, col])
            wind_speed = float(math.hypot(u, v))
            return {
                "lon": round(float(lon), 6),
                "lat": round(float(lat), 6),
                "altitude_agl_m": round(agl, 2),
                "terrain_height_m": round(terrain, 2),
                "altitude_amsl_m": round(terrain + agl, 2),
                "level": grid.level,
                "wind_speed_mps": round(wind_speed, 3),
                "u_mps": round(u, 3),
                "v_mps": round(v, 3),
            }

        waypoints = [waypoint_from_state(simplified[0], self.start)]
        waypoints.extend(waypoint_from_state(state_item) for state_item in simplified[1:-1])
        waypoints.append(waypoint_from_state(simplified[-1], self.end))
        for index, waypoint in enumerate(waypoints, start=1):
            waypoint["seq"] = index
            if index < len(waypoints):
                waypoint["heading_deg"] = round(_bearing_deg((waypoint["lon"], waypoint["lat"]), (waypoints[index]["lon"], waypoints[index]["lat"])), 1)
            elif len(waypoints) > 1:
                waypoint["heading_deg"] = waypoints[-2].get("heading_deg")
            else:
                waypoint["heading_deg"] = None
            waypoint["speed_mps"] = self.config.cruise_speed_mps

        points = [
            [
                item["lon"],
                item["lat"],
                item["altitude_amsl_m"],
                item["altitude_agl_m"],
                item["terrain_height_m"],
            ]
            for item in waypoints
        ]

        samples = []
        cumulative = 0.0
        for index, waypoint in enumerate(waypoints):
            if index > 0:
                previous = waypoints[index - 1]
                cumulative += haversine_km((previous["lon"], previous["lat"]), (waypoint["lon"], waypoint["lat"]))
            heading = waypoint.get("heading_deg")
            if heading is None and index > 0:
                heading = waypoints[index - 1].get("heading_deg")
            if heading is None:
                along = 0.0
                cross = 0.0
            else:
                heading_rad = math.radians(float(heading))
                east = math.sin(heading_rad)
                north = math.cos(heading_rad)
                u = float(waypoint["u_mps"])
                v = float(waypoint["v_mps"])
                along = u * east + v * north
                cross = abs(u * north - v * east)
            sample = {
                "lon": waypoint["lon"],
                "lat": waypoint["lat"],
                "distance_km": round(cumulative, 3),
                "wind_speed": waypoint["wind_speed_mps"],
                "risk": risk_name(float(waypoint["wind_speed_mps"]), self.thresholds),
                "level": waypoint["level"],
                "altitude_agl_m": waypoint["altitude_agl_m"],
                "terrain_height_m": waypoint["terrain_height_m"],
                "altitude_amsl_m": waypoint["altitude_amsl_m"],
                "headwind_component": round(float(along), 2),
                "crosswind_component": round(float(cross), 2),
                "flight_heading": waypoint["heading_deg"],
                "is_tailwind": along >= 0,
            }
            samples.append(sample)

        height_changes = sum(1 for a, b in zip(waypoints, waypoints[1:]) if a["altitude_agl_m"] != b["altitude_agl_m"])
        return {
            "points": points,
            "waypoints": waypoints,
            "segments": [],
            "cost": round(float(best[goal_state]), 3),
            "distance_km": round(cumulative, 3),
            "level": "multi-altitude AGL",
            "altitude_mode": "AGL",
            "altitude_summary": {
                "candidate_levels_m": [float(value) for value in self.levels_m],
                "min_agl_m": round(float(min(item["altitude_agl_m"] for item in waypoints)), 2),
                "max_agl_m": round(float(max(item["altitude_agl_m"] for item in waypoints)), 2),
                "min_amsl_m": round(float(min(item["altitude_amsl_m"] for item in waypoints)), 2),
                "max_amsl_m": round(float(max(item["altitude_amsl_m"] for item in waypoints)), 2),
                "height_changes": height_changes,
            },
            "expanded_nodes": expanded_nodes,
            "analysis_samples": samples,
        }

    def plan(self) -> dict:
        queue: list[tuple[float, float, int, int, int, tuple[int, int] | None]] = []
        best: dict[tuple[int, int, int, tuple[int, int] | None], float] = {}
        parents: dict[tuple[int, int, int, tuple[int, int] | None], tuple[int, int, int, tuple[int, int] | None] | None] = {}
        counter = 0
        for state in self.initial_states():
            start_level_penalty = 0.02 * state[0]
            best[state] = start_level_penalty
            parents[state] = None
            heapq.heappush(queue, (self.heuristic(state[1], state[2]) + start_level_penalty, start_level_penalty, counter, *state))
            counter += 1

        expanded_nodes = 0
        goal_state = None
        while queue:
            _, cost, _, level_index, row, col, previous = heapq.heappop(queue)
            state = (level_index, row, col, previous)
            if cost != best.get(state):
                continue
            if (row, col) == self.end_node:
                goal_state = state
                break
            expanded_nodes += 1
            if expanded_nodes > self.config.max_iterations:
                break
            for direction in NEIGHBOURS_8:
                nr, nc = row + direction[0], col + direction[1]
                if not self.in_bounds(nr, nc):
                    continue
                for next_level in self.level_candidates(level_index):
                    edge = self.edge_cost(level_index, row, col, next_level, nr, nc)
                    if not np.isfinite(edge):
                        continue
                    next_cost = cost + edge + _turn_cost(previous, direction)
                    next_state = (next_level, nr, nc, direction)
                    if next_cost < best.get(next_state, float("inf")):
                        best[next_state] = next_cost
                        parents[next_state] = state
                        counter += 1
                        heapq.heappush(queue, (next_cost + self.heuristic(nr, nc), next_cost, counter, *next_state))

        if goal_state is None:
            raise ValueError(f"多高度层航线规划未找到可行路径，已扩展 {expanded_nodes} 个节点")
        return self.reconstruct(parents, goal_state, best, expanded_nodes)


def plan_multi_altitude_route(
    grids: list[WindGrid],
    start: tuple[float, float],
    end: tuple[float, float],
    thresholds: Thresholds,
    cost_config: Any | None = None,
    config: MultiAltitudeConfig | None = None,
) -> dict:
    return MultiAltitudeAStar(grids, start, end, thresholds, cost_config, config).plan()
