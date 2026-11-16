"""Original spatial planners propose corridors; actual-time checks accept them."""
from dataclasses import replace
import math
import numpy as np

from .grid_planner_lpa_star import LPAStarPlanner
from .grid_planner_wa_lpa_star import WALPAStarPlanner
from .planning_control import checkpoint, stage_budget, PlanningStageExpired
from .planning_limits import seconds_setting
from .wind_time import WIND_TIME_STEP_SECONDS
from .route_service import anchor_route_endpoints, haversine_km
from .routing import plan_route
from .wrf_timeline import iso
from .wrf_native_schema import NATIVE_WRF_TIME_MODEL


def spatial_plan(grid, start, end, thresholds, config, algorithm):
    if algorithm in ("astar", "a_star"):
        return plan_route(grid, start, end, thresholds, cost_config=config)
    cls = LPAStarPlanner if algorithm == "lpa_star" else WALPAStarPlanner
    search = cls(grid, {"lon": start[0], "lat": start[1]}, {"lon": end[0], "lat": end[1]},
                 cost_config=config, thresholds=thresholds)
    result = search.plan()
    if not result.points:
        raise ValueError("候选走廊未找到空间通路")
    return {"points": anchor_route_endpoints(list(result.points), start, end),
            "expanded_nodes": result.expanded_nodes, "planning_time_ms": result.planning_time_ms}


def eta_offsets(grid, points, speed):
    """Project cells onto the ordered proposal, retaining its cumulative clock."""
    lon, lat = np.meshgrid(grid.lons, grid.lats)
    scale = math.cos(math.radians(float(np.mean(grid.lats))))
    best = np.full(lon.shape, np.inf)
    offsets = np.zeros(lon.shape)
    elapsed = 0.
    for a, b in zip(points, points[1:]):
        checkpoint()
        dx, dy = (b[0] - a[0]) * scale, b[1] - a[1]
        denominator = dx * dx + dy * dy
        if denominator == 0:
            continue
        fraction = np.clip(((lon - a[0]) * scale * dx + (lat - a[1]) * dy) / denominator, 0, 1)
        distance = ((lon - a[0]) * scale - fraction * dx) ** 2 + (lat - a[1] - fraction * dy) ** 2
        duration = haversine_km(a[:2], b[:2]) * 1000 / speed
        nearer = distance < best
        offsets[nearer] = elapsed + fraction[nearer] * duration
        best = np.minimum(best, distance)
        elapsed += duration
    return offsets


def proposal_grid(dataset, departure, offsets):
    """A guide only: interpolate each node at its proposed encounter time."""
    first = dataset.frames[0][1]
    stamps = departure + np.broadcast_to(offsets, first.u.shape)
    stamps = np.floor(stamps / WIND_TIME_STEP_SECONDS) * WIND_TIME_STEP_SECONDS
    u, v = np.full(first.u.shape, np.nan), np.full(first.v.shape, np.nan)
    for stamp, grid in dataset.frames:
        exact = np.abs(stamps - stamp) < 1e-6
        u[exact], v[exact] = grid.u[exact], grid.v[exact]
    for (begin, left), (end, right) in zip(dataset.frames, dataset.frames[1:]):
        if dataset.time_model==NATIVE_WRF_TIME_MODEL:
            continue
        if not math.isclose(end - begin, 3600, abs_tol=.001):
            continue
        mask = (stamps > begin) & (stamps < end)
        fraction = (stamps[mask] - begin) / (end - begin)
        u[mask] = left.u[mask] + fraction * (right.u[mask] - left.u[mask])
        v[mask] = left.v[mask] + fraction * (right.v[mask] - left.v[mask])
    return replace(first, u=u, v=v, valid_time=iso(departure), source=f"{dataset.source.upper()} · 候选预计经过时刻")


def corridor_candidates(dataset, request, config, *, on_attempt=None):
    """Three forecast seeds and up to two actual-route ETA refinements each.

    A failed snapshot (including an unsafe goal in that snapshot) is never a
    rejection of the time-dependent request. Every yielded path needs a full
    actual-time check by its caller. Stage budgets cannot consume the whole job.
    """
    departure = request.departure_at.timestamp()
    duration = haversine_km(request.start, request.end) * 1000 / request.average_ground_speed_mps
    seen = set()
    for label, offset in (("departure_corridor", 0.), ("midflight_corridor", duration / 2), ("arrival_corridor", duration)):
        grid = proposal_grid(dataset, departure, offset)
        for iteration in range(3):
            checkpoint()
            phase = label if iteration == 0 else "eta_refinement"
            if on_attempt:
                on_attempt(phase)
            try:
                with stage_budget(seconds_setting("ROUTE_PLAN_CORRIDOR_TIMEOUT_SECONDS", 120)):
                    result = spatial_plan(grid, request.start, request.end, request.thresholds, config, request.planner_type)
            except (ValueError, PlanningStageExpired):
                break
            points = [tuple(point[:2]) for point in result["points"]]
            signature = tuple(points)
            if signature in seen:
                break
            seen.add(signature)
            yield {**result, "points": points, "planner_type": request.planner_type,
                   "search_phase": phase, "search_direction": "forward", "proposal_offset_seconds": offset}
            grid = proposal_grid(dataset, departure, eta_offsets(grid, points, request.average_ground_speed_mps))
