"""Native MSL wind corridors, proposed before expensive V3 shooting pools."""
import math
import numpy as np
from app.wind_provider import WindGrid
from app.wind_corridors import spatial_plan, eta_offsets
from app.models import Thresholds
from app.route_options import build_strategy_cost_config
from app.cost_evaluator import cost_config_from_thresholds
from app.planning_objective import wind_preference
from app.departure_windows import _cell_visits
from .native import VA, iso
from .certificate import shear_limit


def native_guide_cost(provider, points, request):
    """Compare hints after search; the V3 solver still determines real timing."""
    config = cost_config_from_thresholds(Thresholds(**request["thresholds"]), build_strategy_cost_config(request["planning_strategy"],legacy_v3=True))
    z, shear_max = request["altitude_msl_m"], shear_limit(request)
    elapsed, total = 0., 0.
    for a,b in zip(points,points[1:]):
        start=np.array(provider.coordinates.lonlat_to_local(*a))
        end=np.array(provider.coordinates.lonlat_to_local(*b))
        length=float(np.linalg.norm(end-start))
        if length==0:
            continue
        tangent=(end-start)/length
        normal=np.array([-tangent[1],tangent[0]])
        count=max(1,math.ceil(length/500.))
        for fraction in (np.arange(count)+.5)/count:
            xy=start+fraction*(end-start)
            t=elapsed+fraction*length/VA
            w=provider.wind_query(*xy,z,t)
            if not w.get("valid") or math.hypot(w["wx"],w["wy"])>=config.thresholds.max_wind_speed:
                return math.inf
            if math.isfinite(shear_max) and provider.horizontal_shear(*xy,z,t)>=shear_max:
                return math.inf
            wind=np.array([w["wx"],w["wy"]])
            total+=length/count/1000*(config.weights.alpha_distance+wind_preference(
                float(np.linalg.norm(wind)),float(wind@tangent),float(wind@normal),config.weights,config.thresholds))
        elapsed+=length/VA
    return total


def simplify_guides(grid, points, request):
    config = cost_config_from_thresholds(Thresholds(**request["thresholds"]), build_strategy_cost_config(request["planning_strategy"],legacy_v3=True))
    def cost(a, b):
        distance = 0.
        dx, dy = (b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2)), b[1] - a[1]
        phi = math.atan2(dx, dy)
        east, north = math.sin(phi), math.cos(phi)
        for visit in _cell_visits([a, b], grid, VA):
            u, v = float(grid.u[visit.row, visit.col]), float(grid.v[visit.row, visit.col])
            magnitude = math.hypot(u, v)
            if not math.isfinite(magnitude) or magnitude >= config.thresholds.max_wind_speed:
                return math.inf
            distance += (visit.leave - visit.enter) * VA / 1000 * (config.weights.alpha_distance + wind_preference(
                magnitude, u * east + v * north, u * north - v * east, config.weights, config.thresholds))
        return distance
    costs = [cost(a, b) for a, b in zip(points, points[1:])]
    result, index = [points[0]], 0
    while index < len(points) - 1:
        following = index + 1
        for target in range(len(points) - 1, index + 1, -1):
            shortcut = cost(points[index], points[target])
            if math.isfinite(shortcut) and shortcut <= math.fsum(costs[index:target]) + 1e-9:
                following = target
                break
        result.append(points[following])
        index = following
    return result


def proposals(provider, request, check):
    start, goal = request["start"], request["end"]
    scale = math.cos(math.radians((start[1] + goal[1]) / 2))
    span = max(abs(goal[0] - start[0]), abs(goal[1] - start[1]))
    padding = max(.08, min(1., span * .5))
    west, east = min(start[0], goal[0]) - padding, max(start[0], goal[0]) + padding
    south, north = min(start[1], goal[1]) - padding, max(start[1], goal[1]) + padding
    nx = max(9, min(91, math.ceil((east - west) * scale * 111 / 3) + 1))
    ny = max(9, min(91, math.ceil((north - south) * 111 / 3) + 1))
    lons = np.unique(np.r_[np.linspace(west, east, nx), start[0], goal[0]])
    lats = np.unique(np.r_[np.linspace(south, north, ny), start[1], goal[1]])[::-1]
    nx, ny = len(lons), len(lats)
    xy = np.array([[provider.coordinates.lonlat_to_local(float(lon), float(lat)) for lon in lons] for lat in lats])
    distance = float(np.linalg.norm(np.array(provider.coordinates.lonlat_to_local(*goal)) - np.array(provider.coordinates.lonlat_to_local(*start))))
    duration = distance / VA
    z = request["altitude_msl_m"]
    config = build_strategy_cost_config(request["planning_strategy"],legacy_v3=True)
    thresholds = Thresholds(**request["thresholds"])
    shear_max = shear_limit(request)
    template = WindGrid(lons, lats, np.zeros((ny, nx)), np.zeros((ny, nx)), provider.spec["cycle"], 0,
                        f"{z:g}m MSL", iso(provider.epoch), "V3 NATIVE GUIDE ONLY")
    seen = set()
    for offset in (0., duration / 2, duration):
        clock = np.full((ny, nx), offset)
        for refinement in range(2):
            check()
            u, v = np.full((ny, nx), np.nan), np.full((ny, nx), np.nan)
            # Query in time order to retain the adjacent-hour native LRU.
            for index in np.argsort(clock, axis=None):
                check()
                j, i = np.unravel_index(index, clock.shape)
                q = provider.diagnostic_query(*xy[j, i], z, float(clock[j, i]))
                if q.get("valid"):
                    if math.isfinite(shear_max) and provider.horizontal_shear(*xy[j,i],z,float(clock[j,i])) >= shear_max:
                        continue
                    # Leave room for native-cell interpolation and a physical
                    # turn. Exact endpoints retain their actual point checks.
                    endpoint = any(abs(lons[i]-p[0])<1e-10 and abs(lats[j]-p[1])<1e-10 for p in (start, goal))
                    if not endpoint:
                        blocked = False
                        for dx,dy in ((1500.,0.),(-1500.,0.),(0.,1500.),(0.,-1500.),(1500.,1500.),(-1500.,1500.),(1500.,-1500.),(-1500.,-1500.)):
                            x,y = xy[j,i] + [dx,dy]
                            if (not provider.wind_query(x,y,z,float(clock[j,i])).get("valid")
                                    or math.isfinite(shear_max) and provider.horizontal_shear(x,y,z,float(clock[j,i])) >= shear_max):
                                blocked = True
                                break
                        if blocked:
                            continue
                    u[j, i], v[j, i] = q["earth_diagnostic"]["east"], q["earth_diagnostic"]["north"]
            grid = WindGrid(lons, lats, u, v, template.cycle, 0, template.level, template.valid_time, template.source)
            try:
                route = spatial_plan(grid, tuple(start), tuple(goal), thresholds, config, "astar")
            except ValueError:
                break
            points = [tuple(p[:2]) for p in route["points"]]
            if tuple(points) in seen:
                break
            seen.add(tuple(points))
            # Skip cells only when a full guide-grid cell traversal proves the
            # shortcut safe and no more costly. V3 still validates the actual
            # continuous native trajectory independently after connection.
            simplified = simplify_guides(grid, points, request)
            # Remove residual collinear cells without dropping wind detours.
            local = [np.array(provider.coordinates.lonlat_to_local(*point)) for point in simplified]
            keep = [0]
            for index in range(1, len(local) - 1):
                a, b = local[index] - local[index - 1], local[index + 1] - local[index]
                if np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0:
                    continue
                angle = math.atan2(float(a[0] * b[1] - a[1] * b[0]), float(a @ b))
                if abs(angle) > .02:
                    keep.append(index)
            keep.append(len(local) - 1)
            spatial_points=[simplified[i] for i in keep]
            direct=[tuple(start),tuple(goal)]
            # This comparison occurs only after the selected spatial search.
            # It prevents coarse guide-cell clearance buffers from forcing a
            # needless short detour when the actual native straight hint wins.
            if len(spatial_points)>2:
                direct_cost=native_guide_cost(provider,direct,request)
                if math.isfinite(direct_cost) and direct_cost<=native_guide_cost(provider,spatial_points,request):
                    yield {"points":direct,"phase":"native_searched_shortcut","offset_seconds":offset,"spatial_nodes":len(points)}
            yield {"points": spatial_points, "phase": "native_eta_corridor" if refinement else "native_forecast_corridor",
                   "offset_seconds": offset, "spatial_nodes": len(points)}
            clock = eta_offsets(template, points, VA)


def states_for_path(provider, points, z, start_heading, end_heading):
    local = [np.array(provider.coordinates.lonlat_to_local(*p[:2])) for p in points]
    states = []
    elapsed = 0.
    previous = start_heading
    for index, point in enumerate(local):
        if index:
            elapsed += float(np.linalg.norm(point - local[index - 1])) / VA
        tangent = (local[min(index + 1, len(local) - 1)] - local[max(0, index - 1)])
        phi = math.atan2(tangent[1], tangent[0])
        q = provider.wind_query(*point, z, elapsed)
        if not q.get("valid"):
            raise ValueError("V3 引导点预计经过时间或高度数据不足")
        cross = -math.sin(phi) * q["wx"] + math.cos(phi) * q["wy"]
        if abs(cross) >= VA:
            raise ValueError("V3 引导点无法补偿侧风")
        psi = phi - math.asin(cross / VA)
        if index == 0 and start_heading is not None:
            psi = start_heading
        if index == len(local) - 1 and end_heading is not None:
            psi = end_heading
        if previous is not None:
            psi += 2 * math.pi * round((previous - psi) / (2 * math.pi))
        states.append([*point.tolist(), psi])
        previous = psi
    return states
