import math
from app.route_refinement import RouteRefiner, cycle_proposals, shape_quality
from test_temporal_routing import dataset, request, BASE
from app.local_weather import LocalWeather
from app.route_options import build_strategy_cost_config


class ProbeWeather:
    def __init__(self, evaluate):
        self.evaluate = evaluate
        self.grid = dataset().frames[0][1]

    def route_cost(self, points):
        return self.evaluate(points)


def test_safe_loop_is_removed_even_if_original_objective_increases():
    points = [(0., 0.), (.1, 0.), (.2, 0.), (.1, 0.), (.3, 0.)]
    weather = ProbeWeather(lambda p: 10. if any(p[i] in p[:i] for i in range(len(p))) else 20.)
    result, cost, diagnostics = RouteRefiner(weather, points).finish()
    assert result == [(0., 0.), (.3, 0.)]
    assert cost == 20 and diagnostics['cycles_removed'] > 0


def test_deletion_unsafe_at_earlier_passage_time_keeps_late_flight_branch():
    points = [(0., 0.), (.1, 0.), (.2, 0.), (.1, 0.), (.3, 0.)]
    weather = ProbeWeather(lambda p: 10. if any(p[i] in p[:i] for i in range(len(p))) else math.inf)
    result, cost, diagnostics = RouteRefiner(weather, points).finish()
    assert cost == 10 and diagnostics['cycles_removed'] == 0
    assert any(result[i] in result[:i] for i in range(len(result)))


def test_crossing_and_partial_collinear_return_have_cycle_proposals():
    for points in [[(0., 0.), (.1, .1), (.0, .1), (.1, .0), (.2, .0)],
                   [(0., 0.), (.2, .0), (.1, .0), (.3, .1)]]:
        proposals = cycle_proposals(points)
        assert proposals
        assert all(p[0] == points[0] and p[-1] == points[-1] for p in proposals)


def test_ordinary_shortcut_never_increases_cost_or_bends():
    data, req = dataset(), request()
    weather = LocalWeather(data, req, build_strategy_cost_config(req.planning_strategy))
    points = [(0., 0.), (.1, .1), (.2, 0.), (.3, 0.), (.6, 0.)]
    before, quality = weather.route_cost(points), shape_quality(points)
    result, cost, diagnostics = RouteRefiner(weather, points).finish()
    assert cost <= before and weather.route_cost(result) == cost
    after = shape_quality(result)
    assert all(after[key] <= quality[key] + 1e-6 for key in ['max_turn_deg','turn_count','total_turn_deg'])


def test_refinement_is_resumable_in_small_slices():
    weather = ProbeWeather(lambda p: len(p))
    refiner = RouteRefiner(weather, [(0., 0.), (.1, .1), (.2, 0.), (.3, 0.)])
    assert not refiner.run_slice(seconds=0)
    assert refiner.finish()[0][0] == (0., 0.)


def test_same_weather_cell_without_spatial_crossing_is_not_a_cycle():
    points = [(0., 0.), (.01, .01), (.02, .005), (.03, .015)]
    assert not cycle_proposals(points)


def test_search_incumbent_uses_geometry_and_cost_returned_by_acceptance():
    from app.segmented_routing import SegmentedPlanner
    data, req = dataset(), request()
    weather = LocalWeather(data, req, build_strategy_cost_config(req.planning_strategy))
    search = SegmentedPlanner(weather, req.start, req.end, [-.1,-.3,.7,.3])
    search.algorithm = 'wa_lpa_star'
    direct = [req.start, req.end]
    accepted = {'points': direct, 'objective_cost': weather.route_cost(direct),
                'distance_km': weather.edge(*direct).distance_km, 'analysis': {'navigation_allowed': True}}
    search.accept = lambda value: accepted
    search._candidate([req.start,(.2,.1),req.end], 'probe')
    assert search.best is accepted
    assert search.best_cost == accepted['objective_cost']
    assert search.best['points'] == direct
