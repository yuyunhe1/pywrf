"""Small exhaustive oracles and exact interval/boundary checks for new search."""
from dataclasses import replace
from datetime import timedelta
import math

import numpy as np
import pytest

from app.departure_windows import Interval, prepare_evaluation
from app.local_weather import LocalWeather
from app.route_options import build_strategy_cost_config
from app.segmented_routing import Backward, Forward, SegmentedPlanner
from app.wind_time import frame_time
from test_temporal_routing import BASE, dataset, request, profile


class Graph:
    def __init__(self, points, edges):
        self.points, self.edges, self.start, self.goal = points, edges, "start", "goal"

    def point(self, node):
        return self.points[node]

    def neighbours(self, node):
        return self.edges.get(node, [])

    def predecessors(self, node):
        return [a for a, bs in self.edges.items() if node in bs]

    def all_paths(self, node="start"):
        if node == self.goal:
            yield [self.point(node)]
        for other in self.neighbours(node):
            for suffix in self.all_paths(other):
                yield [self.point(node), *suffix]


def make_search(data, req, graph=None):
    weather = LocalWeather(data, req, build_strategy_cost_config(req.planning_strategy))
    search = SegmentedPlanner(weather, req.start, req.end, [-.1, -.3, .7, .3], graph=graph)
    return search


@pytest.mark.parametrize("algorithm", ["astar", "lpa_star", "wa_lpa_star"])
def test_exhaustive_dag_later_arrival_and_dead_end_backtracking(algorithm):
    def wind(hour, u, v):
        u[:, 4] = 10 if hour < 2 else 1
        u[3, 0] = 12  # dead branch is separate from the early branch's closed-cell corner
    data = dataset(wind)
    req = request().model_copy(update={"end": (.4, 0.)})
    graph = Graph({"start": (0., 0.), "dead": (.0, -.1), "early": (.1, .1),
                   "late": (.1, .2), "join": (.2, .1), "goal": (.4, 0.)},
                  {"start": ["dead", "early", "late"], "dead": [], "early": ["join"],
                   "late": ["join"], "join": ["goal"]})
    search = make_search(data, req, graph)
    search.calibrate = lambda *a: None  # oracle is this directed graph, without extra bridges
    feasible = [points for points in graph.all_paths() if math.isfinite(search.weather.route_cost(points))]
    assert feasible, "oracle fixture must contain a later, safe branch"
    result = search.search(algorithm)
    assert math.isfinite(search.weather.route_cost(result["points"]))
    assert profile(data, result, req)["decision"]["status"] == "allowed"
    assert len({label.stamp for label in search.forward_at["join"]}) >= 2
    assert search.stats["backward_expanded"] > 0
    assert search.backward_at


def test_same_window_with_another_suffix_is_retained_for_connection_and_comparison():
    search = make_search(dataset(), request())
    search.algorithm = "astar"
    window = Interval(BASE.timestamp() + 1000, BASE.timestamp() + 1600)
    parent_a = Backward(search.graph.goal, window, 0., 0.)
    parent_b = Backward((2, 1), window, 0., 0.)
    first = Backward(search.graph.start, window, 10., 20., parent_a)
    second = Backward(search.graph.start, window, 12., 25., parent_b)
    search._offer_b(first)
    search._offer_b(second)
    assert search.backward_at[search.graph.start] == [first, second]
    assert search.stats['retained_suffix_alternatives'] == 1
    assert len(search.alternative_queue) == 1


def test_spatial_overlap_without_time_overlap_never_joins():
    search = make_search(dataset(), request())
    search.algorithm = "astar"
    start = search.graph.start
    f = Forward(start, search.start, BASE.timestamp(), 0., 0.)
    b = Backward(start, Interval(BASE.timestamp() + 600, BASE.timestamp() + 1200), 0., 0.)
    search.connect(f, b)
    assert search.best is None and search.stats["connection_attempts"] == 0


@pytest.mark.parametrize("algorithm", ["astar", "lpa_star", "wa_lpa_star"])
def test_non_aligned_departure_short_segments_and_frame_crossings_add_no_wait(algorithm):
    req = request().model_copy(update={"departure_at": BASE + timedelta(minutes=3), "end": (.15, 0.)})
    search = make_search(dataset(), req)
    result = search.search(algorithm)
    for labels in search.forward_at.values():
        for label in labels:
            if not label.parent:
                continue
            duration = label.stamp - label.parent.stamp
            assert duration <= frame_time(label.parent.stamp) + 600 - label.parent.stamp + 1e-5
            distance = search.distance(label.parent.point, label.point)
            assert duration == pytest.approx(distance * 1000 / search.weather.speed, abs=1e-5)
    value = profile(dataset(), result, req)
    assert value["flight_duration_seconds"] == pytest.approx(value["analysis"]["total_distance_km"] * 100)
    short = request().model_copy(update={"departure_at": req.departure_at, "end": (.005, 0.)})
    short_search = make_search(dataset(), short)
    short_result = short_search.search(algorithm)
    assert profile(dataset(), short_result, short)["flight_duration_seconds"] < 420


def test_reverse_interval_recurrence_matches_exhaustive_departure_checks_and_forward_wind_direction():
    def wind(hour, u, v):
        u[:] = 2 if hour != 1 else 12
    data, req = dataset(wind), request()
    weather = make_search(data, req).weather
    edge = weather.edge((.0, .0), (.1, .0))
    domain = Interval(BASE.timestamp(), BASE.timestamp() + 4 * 3600)
    safe = weather.safe_departures(edge, domain)
    # Ends are safe; unsafe middle must remain excluded, not interpolated away.
    assert any(x.contains(domain.start) for x in safe)
    assert any(x.contains(domain.end) for x in safe)
    assert not any(x.contains(BASE.timestamp() + 3600) for x in safe)
    evaluation = prepare_evaluation(edge.points, list(data.frames), weather.speed, 7.9, None)
    for offset in range(0, 4 * 3600 + 1, 37):
        stamp = BASE.timestamp() + offset
        expected = not any(issue.departure.contains(stamp) for issue in evaluation.issues)
        assert any(x.contains(stamp) for x in safe) == expected
        assert edge.allowed(stamp) == expected
    east = weather.cost(edge, domain.start)
    west = weather.cost(weather.edge(edge.points[1], edge.points[0]), domain.start)
    assert west > east  # reverse search must score A->B, not B->A


@pytest.mark.parametrize("speed", [10, 50])
def test_reachable_region_varies_with_speed_resolution_and_keeps_shear_halo(speed):
    data, req = dataset(), request(average_ground_speed_mps=speed)
    grid = data.frames[0][1]
    grid = replace(grid, lons=np.linspace(0., .6, 121), lats=np.linspace(.2, -.2, 81),
                   u=np.ones((81, 121)), v=np.zeros((81, 121)))
    data = replace(data, dataset_id=f"region-{speed}", frames=tuple((stamp, replace(grid, valid_time=g.valid_time)) for stamp, g in data.frames))
    weather = LocalWeather(data, req, build_strategy_cost_config(req.planning_strategy), loading_mode="local")
    weather.read_region((.3, 0.), BASE.timestamp())
    expected = weather.stats["maximum_region_cells"]
    assert expected > 16
    assert weather.diagnostics()["reach_radius_m"] == 600 * speed
    before = weather.stats["local_block_reads"]
    weather.read_region((.3, 0.), BASE.timestamp())
    assert weather.stats["local_block_reads"] == before
    assert weather.diagnostics()["shear_halo_cells"] == 1


def test_native_reverse_can_discover_later_intervals_without_pinning_one_eta(monkeypatch):
    req = request().model_copy(update={"end": (.2, .0)})
    search = make_search(dataset(), req)
    search.algorithm = "astar"
    search.next_arrival = search.eta_hint
    assert search.enable_arrivals()
    first = search.backward_at[search.graph.goal][0].window
    assert search.enable_arrivals()
    second = search.backward_at[search.graph.goal][1].window
    assert second.start == first.end and second.end > first.end
