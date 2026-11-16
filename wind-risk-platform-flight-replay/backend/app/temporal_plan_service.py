"""Publish only fully validated spatiotemporal routes and their frozen context."""
import math
import time
from .flight_replay import build_profile
from .gfs_provider import _current_utc
from . import gfs_provider
from . import wrf_cache_provider as wrf_provider
from .models import FlightProfileRequest
from .planning_control import PlanningStageExpired, PlanningCancelled, checkpoint, stage_budget, current_control, planning_scope
from .planning_limits import integer_setting, seconds_setting
from .wind_time import WIND_TIME_STEP_SECONDS, WIND_TIME_MODEL
from .wrf_native_schema import NATIVE_WRF_TIME_MODEL
from .route_context import geometry_route
from .route_service import haversine_km
from .route_jobs import candidate_score
from .route_options import (build_strategy_cost_config, describe_aircraft_model, describe_strategy,
                            normalize_aircraft_model, normalize_planning_strategy)
from .local_weather import LocalWeather
from .segmented_routing import SegmentedPlanner, MODEL as SEARCH_MODEL
from .temporal_routing import TemporalSearchExhausted
from .wrf_timeline import get_dataset, iso, load_dataset, load_gfs_dataset, vector_at
from .planning_objective import CLASSIC_OBJECTIVE_MODEL as OBJECTIVE_MODEL
from .grid_coverage import SPATIAL_RISK_MODEL
from .route_refinement import RouteRefiner, shape_quality
from .parallel_routing import ParallelPlanner
from .route_quality import assess_route_quality, simple_geometry_seed, MODEL as QUALITY_MODEL


def is_temporal(request):
    return request.time_aware or request.departure_at is not None


def prepare_temporal_request(request):
    """Resolve 'now' and pin arrays BEFORE a job waits in the worker queue."""
    now = _current_utc()
    source = request.source or "wrf"
    if request.dataset_id:
        dataset = get_dataset(request.dataset_id)
    elif source == "gfs":
        cycle = request.cycle or (gfs_provider.find_file_by_valid_time(request.valid_time).cycle if request.valid_time else None)
        dataset = load_gfs_dataset(request.level, now, cycle)
    else:
        cycle = request.cycle or (wrf_provider._find_record_by_valid_time(request.valid_time)['cycle'] if request.valid_time else None)
        dataset = load_dataset(request.level, now, cycle) if cycle else load_dataset(request.level, now)
    if dataset.source != source:
        raise ValueError("规划数据源与所选风场不一致，请重新评估")
    if dataset.level != request.level:
        raise ValueError("风场评估高度与航线高度不一致，请重新评估")
    request = request.model_copy(update={"time_aware": True, "departure_at": request.departure_at or now,
                                         "dataset_id": dataset.dataset_id, "source": source})
    # Reuse the finite risk-limit validation of the fixed-route APIs.
    FlightProfileRequest(**request.model_dump(include={"departure_at", "average_ground_speed_mps", "thresholds", "wind_shear"}))
    return request, dataset


def compute_temporal_plan(request, dataset, bbox, max_grid_cells, publish=None, candidate_validator=None):
    checkpoint()
    strategy = normalize_planning_strategy(request.planning_strategy)
    aircraft = normalize_aircraft_model(request.aircraft_model)
    planner_type = request.planner_type.lower().replace("-", "_")
    if planner_type not in ("astar", "a_star", "lpa_star", "wa_lpa_star"):
        raise ValueError("不支持的规划算法")
    config = build_strategy_cost_config(strategy)
    objective_model = 'PEAK_CAP_DISTANCE_V1' if request.minimize_distance_under_peak else OBJECTIVE_MODEL
    profile_request = FlightProfileRequest(departure_at=request.departure_at, average_ground_speed_mps=request.average_ground_speed_mps,
                                           thresholds=request.thresholds, wind_shear=request.wind_shear, dataset_id=dataset.dataset_id)
    best = None
    label = dataset.source.upper()
    began = time.monotonic()
    diagnostics = {"attempts": [], "first_feasible_seconds": None, "direct_preview_published": False}
    limited_reason = None
    parent = current_control()
    quality = dict(deadline=None,started=None,rejected_candidates=0,resolved=False)
    diagnostics['route_quality'] = quality

    class QualityCancel:
        def is_set(self):
            if parent and parent.stage_deadline is not None and time.monotonic()>=parent.stage_deadline:
                raise PlanningStageExpired()
            return bool(parent and parent.cancel.is_set()) or bool(
                best is None and quality['deadline'] is not None and time.monotonic() >= quality['deadline'])

    def quality_progress(status, assessment=None):
        if parent is None or parent.progress is None: return
        message = {'searching_alternative':'检测到折返或局部盘旋，暂停绘制并额外搜索最多10秒',
                   'resolved':'已找到不含明显折返或盘旋的完整可行路线',
                   'retry_exhausted':'额外搜索10秒后，仍未确认合适的非盘旋路线'}.get(status,status)
        parent.progress({'route_quality':dict(model=QUALITY_MODEL,status=status,message=message,
            retry_seconds=request.route_quality_extra_seconds,retry_until_monotonic=quality['deadline'],
            rejected_candidates=quality['rejected_candidates'],assessment=assessment,
            departure_at=request.departure_at.isoformat())})

    def empty(status, reason):
        return {"points": [], "segments": [], "reference_route": False, "time_aware": True,
                "planner_type": planner_type, "requested_planner_type": planner_type,
                "planning_strategy": strategy, "route_status": status, "planning_message": reason,
                "level": request.level, "dataset_id": dataset.dataset_id, "source": dataset.source,
                "spatial_risk_model": SPATIAL_RISK_MODEL, "objective_model": objective_model,
                "analysis": {"samples": [], "navigation_allowed": False,
                             "navigation_status": "unknown" if status in {"data_insufficient","quality_unconfirmed"} else "blocked",
                             "navigation_decision_reason": reason}, "planning_diagnostics": diagnostics}

    if not dataset.frames:
        return empty("data_insufficient", dataset.error or f"缺少 {label} 风场时次")
    start_wind = vector_at(dataset, request.departure_at.timestamp(), *request.start)
    if start_wind is None:
        return empty("data_insufficient", f"起飞时刻的起点缺少 {label} 风场，未生成航线")
    if math.hypot(*start_wind) >= request.thresholds.danger:
        return empty("no_feasible_route", "起飞时刻的起点风速达到硬上限，无法起飞")
    departure = request.departure_at.timestamp()
    anchors = request.required_waypoints or [request.start, request.end]
    earliest = departure + sum(haversine_km(a, b) for a, b in zip(anchors, anchors[1:])) * 1000 / request.average_ground_speed_mps
    if earliest > dataset.frames[-1][0] or any(
        end - begin > (600 if dataset.time_model==NATIVE_WRF_TIME_MODEL else 3600)+.001 and begin + WIND_TIME_STEP_SECONDS < earliest and end > departure
        for (begin, _), (end, _) in zip(dataset.frames, dataset.frames[1:])
    ):
        return empty("data_insufficient", f"{label} 预报时次缺失或未覆盖最短航程的到达时刻，未生成航线")
    try:
        weather = LocalWeather(dataset, request, config)
    except MemoryError as exc:
        return empty("resource_limit", str(exc))
    diagnostics["weather_loading_mode"] = weather.loading_mode
    diagnostics["full_field_load_seconds"] = weather.stats["full_field_load_seconds"]
    if request.peak_wind_limit_mps is not None:
        from .grid_coverage import point_cells
        cells = point_cells(weather.grid, request.start)
        if any(weather.vector(departure, *cell) is None for cell in cells):
            return empty('data_insufficient', '起飞时刻的起点接触网格缺少风场数据')
        if any(weather._danger(departure, cell) for cell in cells):
            return empty('peak_cap_blocked', '起点接触网格已超过本次峰值搜索上限；不据此否定其他峰值上限或起飞时刻')
    if integer_setting("ROUTE_PLAN_SEARCH_FULL_DOMAIN", 1):
        grid = weather.grid
        bbox = [float(min(grid.lons)), float(min(grid.lats)), float(max(grid.lons)), float(max(grid.lats))]
    try:
        planner_class = ParallelPlanner if integer_setting("ROUTE_PLAN_DIRECTION_PROCESSES", 2) == 2 else SegmentedPlanner
        planner = planner_class(weather, request.start, request.end, bbox, max_grid_cells)
    except ValueError as exc:
        return empty("data_insufficient", str(exc))

    def describe(candidate, cost):
        route = geometry_route(candidate["points"], request.level, source=dataset.source)
        profile = build_profile(route, profile_request, dataset)
        peak = weather.contact_peak(candidate['points']) if request.required_waypoints else profile['analysis']['max_wind_speed']
        if request.required_waypoints:
            profile['analysis']['interpolated_max_wind_speed'] = profile['analysis'].get('max_wind_speed')
            profile['analysis']['max_contact_wind_speed_mps'] = peak
            profile['analysis']['max_wind_speed'] = peak
            profile['analysis']['peak_wind_model'] = 'CLOSED_CELL_CONTACT_PEAK_V1'
        profile["reference_route"] = False
        context = {**route, "dataset_id": dataset.dataset_id, "departure_at": profile["departure_at"]}
        return {**candidate, "time_aware": True, "requested_planner_type": planner_type,
                "aircraft_model": aircraft, "aircraft_model_label": describe_aircraft_model(aircraft),
                "planning_strategy": strategy, "planning_strategy_label": describe_strategy(strategy),
                "cost_config": config, "search_bbox": bbox, "multi_altitude": False,
                "reference_route": False, "route_status": "feasible", "planning_message": (
                    "首条可行航线已通过全程验收，搜索结束" if request.stop_on_first_feasible else "已找到跨时刻可行避风航线，继续优化"),
                "stop_on_first_feasible": request.stop_on_first_feasible,
                "objective_cost": cost, "objective_model": objective_model, "time_model": "constant_ground_speed",
                "required_waypoints": request.required_waypoints,
                "max_contact_wind_speed_mps": peak,
                "spatial_risk_model": SPATIAL_RISK_MODEL,
                "wind_time_step_seconds": WIND_TIME_STEP_SECONDS, "wind_time_model": dataset.time_model,
                "departure_at": profile["departure_at"], "arrival_at": profile["arrival_at"], "source": dataset.source,
                "average_ground_speed_mps": request.average_ground_speed_mps, "dataset_id": dataset.dataset_id,
                "analysis": profile["analysis"], "segments": profile["segments"],
                "distance_km": profile["analysis"]["total_distance_km"], "level": request.level,
                "route_context": context, "flight_profile": profile, "departure_windows": None,
                "planning_diagnostics": diagnostics,
                "metadata": {"source": f"{label} · 跨时刻规划", "cycle": dataset.cycle, "level": request.level,
                             "valid_time": iso(request.departure_at.timestamp()), "dataset_id": dataset.dataset_id}}

    def accept(candidate):
        nonlocal best
        checkpoint()
        cost = weather.route_cost(candidate["points"])
        if not math.isfinite(cost):
            return
        points, cost, refinement = RouteRefiner(weather, candidate["points"], prefer_simple=request.prefer_simple_route).finish()
        candidate = {**candidate, "points": points, "refinement": refinement, "shape_quality": shape_quality(points)}
        # A straight route is considered only AFTER a real corridor search.
        # It must improve the very same objective and pass continuous hazards.
        direct_cost = weather.route_cost([request.start, request.end])
        if direct_cost <= cost:
            cost = direct_cost
            candidate = {**candidate, "points": [request.start, request.end], "search_phase": "searched_shortcut"}
            candidate["shape_quality"] = shape_quality(candidate["points"])
        assessment = assess_route_quality(candidate['points'],request.average_ground_speed_mps)
        candidate['display_quality'] = assessment
        if request.prefer_simple_route and not assessment['acceptable']:
            quality['rejected_candidates'] += 1
            quality['last_rejected'] = assessment
            if quality['started'] is None:
                quality['started'] = time.monotonic()
                quality['deadline'] = quality['started'] + request.route_quality_extra_seconds
                quality_progress('searching_alternative',assessment)
            seeds=quality.setdefault('simple_seeds',[])
            if len(seeds)<3:
                seed=simple_geometry_seed(candidate['points'],request.average_ground_speed_mps)
                if seed and seed not in seeds: seeds.append(seed)
            return None
        if best is not None and cost > best["objective_cost"] + 1e-9:
            return
        result = describe(candidate, cost)
        if request.max_route_distance_km is not None and result['distance_km'] > request.max_route_distance_km+1e-8:
            return None
        if candidate_validator is not None and not candidate_validator(result):
            return None
        # A preview, a thinned edge and a final route must all pass the same
        # continuous, original-grid evaluation used by saved routes.
        if result["analysis"]["navigation_allowed"] and (best is None or candidate_score(result) < candidate_score(best)):
            best = result
            if diagnostics["first_feasible_seconds"] is None:
                diagnostics["first_feasible_seconds"] = time.monotonic() - began
            resolved = quality['deadline'] is not None
            if resolved:
                quality['resolved'] = True
                quality['extra_search_seconds'] = time.monotonic()-quality['started']
                quality['deadline'] = None
            if publish:
                publish(result)
            if resolved:
                quality_progress('resolved',assessment)
            return result
        return None

    attempt = {"phase": "segmented_bidirectional_search", "algorithm": planner_type}
    diagnostics["attempts"].append(attempt)
    try:
        with planning_scope(parent.deadline if parent else float('inf'),QualityCancel(),
                            progress=parent.progress if parent else None):
            candidate = planner.search(planner_type, accept=accept)
        attempt.update(planner.diagnostics())
        if best is not None:
            best.update({key: candidate[key] for key in ("expanded_nodes", "arrival_labels", "planning_time_ms")})
    except PlanningCancelled:
        if parent and getattr(parent.cancel,'cancelled_by_owner',parent.cancel.is_set)(): raise
        if quality['deadline'] is None or time.monotonic()<quality['deadline']: raise
        quality['extra_search_seconds'] = time.monotonic()-quality['started']
        quality_progress('retry_exhausted',quality.get('last_rejected'))
        attempt.update(planner.diagnostics(),stop_reason='route_quality_retry_exhausted')
        return empty('quality_unconfirmed','已额外搜索10秒，尚未确认不含明显折返或局部盘旋的可行路线')
    except TemporalSearchExhausted as exc:
        attempt.update(exc.diagnostics, message=str(exc))
        limited_reason = str(exc)
        if best is None:
            status = ("no_feasible_route" if exc.diagnostics.get("limit_type") == "exhausted_graph" else "resource_limit")
            if quality['rejected_candidates'] and status=='no_feasible_route': status='quality_unconfirmed'
            if weather.failures or weather.stats["missing_cell_vectors"]:
                status = "data_insufficient"
            reason = (f"{label} 小时数据、变量或局部覆盖不足，尚未确认可行航线：" + "; ".join(sorted(weather.failures)[:2])
                      if status == "data_insufficient" else str(exc))
            if status=='quality_unconfirmed': reason='当前搜索仅找到含明显折返或盘旋的候选，尚未确认合适的可行路线'
            return empty(status, reason)
    diagnostics["search_model"] = SEARCH_MODEL
    diagnostics["weather"] = weather.diagnostics()
    if best:
        best["planning_message"] = ("首条可行航线已通过全程验收，搜索结束" if request.stop_on_first_feasible else
            f"{limited_reason}；保留已验证的最佳航线" if limited_reason else "规划完成：当前已验证的最佳航线")
        return best
    return empty("no_feasible_route", "当前时空图中未找到满足全程约束的航线")
