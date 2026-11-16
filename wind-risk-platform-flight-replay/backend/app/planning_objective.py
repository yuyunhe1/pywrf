"""One distance-integrated wind objective for proposals and published routes."""
import math
from .cost_evaluator import cost_config_from_thresholds, normalize_risk
from .route_options import build_strategy_cost_config
from .wind_time import frame_boundaries

OBJECTIVE_MODEL = "SUPERCOVER_WIND_CONTACT_V2"
CLASSIC_OBJECTIVE_MODEL = "SUPERCOVER_WIND_EXPOSURE_V5"


def wind_preference(magnitude, along, cross, weights, thresholds):
    return (weights.beta_wind * normalize_risk(magnitude, thresholds.safe_wind_speed, thresholds.max_wind_speed)
            + weights.epsilon_wind_exposure * normalize_risk(magnitude,0.,thresholds.max_wind_speed)**2
            + weights.gamma_headwind * max(0., -along)
            + weights.delta_crosswind * normalize_risk(abs(cross), 0., thresholds.max_wind_speed))


def edge_objective(edge, departure, vector_at_cell, config):
    """Worst simultaneous cell exposure plus a grid-scale crossing-risk toll.

    Never average opposing cells into calm wind or give point contacts zero
    soft cost. Distance and flight time remain the actual geometric values.
    """
    if edge.duration <= 0:
        return 0.
    a, b = edge.points
    heading = math.atan2((b[0]-a[0])*math.cos(math.radians((a[1]+b[1])/2)), b[1]-a[1])
    east, north = math.sin(heading), math.cos(heading)
    def worst(stamp, cells):
        values = []
        for row, col in cells:
            vector = vector_at_cell(stamp, row, col)
            if vector is None:
                return math.inf
            u, v = vector
            values.append(wind_preference(math.hypot(u,v), u*east+v*north,
                                          abs(u*north-v*east), config.weights, config.thresholds))
        return max(values, default=math.inf)
    cost = edge.distance_km * config.weights.alpha_distance
    for begin, finish, cells in edge.spans:
        cuts = [begin, *(t-departure for t in frame_boundaries(departure+begin, departure+finish)), finish]
        for left, right in zip(cuts, cuts[1:]):
            cost += edge.distance_km * (right-left)/edge.duration * worst(departure+(left+right)/2, cells)
    for contact in edge.contacts:
        cost += contact.exposure_km * worst(departure+contact.offset, contact.cells)
    return cost


def profile_objective(profile, strategy, thresholds):
    """Integrate the same objective along the actual, retimed V3 trajectory.

    Samples include every dense-polynomial boundary; timing and hard-constraint
    acceptance are independent of this soft-cost quadrature.
    """
    config = cost_config_from_thresholds(thresholds, build_strategy_cost_config(strategy,legacy_v3=True))
    previous = None
    total = 0.
    for sample in profile["analysis"]["samples"]:
        values = [sample.get(key) for key in ("distance_km", "wind_speed", "headwind_component", "crosswind_component")]
        if any(value is None or not math.isfinite(value) for value in values):
            return math.inf
        distance, magnitude, along, cross = values
        value = config.weights.alpha_distance + wind_preference(magnitude, along, cross, config.weights, config.thresholds)
        if previous is not None:
            total += max(0., distance - previous[0]) * (value + previous[1]) / 2
        previous = distance, value
    return total if previous else math.inf
