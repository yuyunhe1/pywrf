from threading import Event, Thread
from time import monotonic
import pytest

from app.parallel_routing import ParallelPlanner
from app.direction_pool import direction_pool, shutdown_direction_pool
from app.local_weather import LocalWeather
from app.planning_control import planning_scope, PlanningCancelled
from app.route_options import build_strategy_cost_config
from test_temporal_routing import dataset, request, profile
from dataclasses import replace


@pytest.fixture(scope='module', autouse=True)
def stop_workers():
    yield
    shutdown_direction_pool()


def search_for(data, req):
    return ParallelPlanner(LocalWeather(data, req, build_strategy_cost_config(req.planning_strategy)), req.start, req.end, [-.1,-.3,.7,.3])


@pytest.mark.parametrize('algorithm', ['astar','lpa_star','wa_lpa_star'])
def test_real_parallel_processes_share_safe_frozen_weather_and_persist(algorithm):
    def wind(hour,u,v):
        u[:] = 2
        if hour >= 1:
            u[2,3] = 12
    data, req = dataset(wind), request(planner_type=algorithm)
    search = search_for(data, req)
    with planning_scope(monotonic()+20, Event()):
        result = search.search(algorithm)
    assert profile(data, result, req)['decision']['status'] == 'allowed'
    workers = result['direction_workers']
    assert workers['forward']['pid'] != workers['backward']['pid']
    assert result['observed_parallel_overlap_seconds'] > 0
    assert result['forward_expanded'] and result['backward_expanded']
    assert all(item['process'].is_alive() for item in direction_pool().workers.values())
    assert result['worker_release_seconds'] < 2
    assert result['weather']['weather_loading_mode'] == 'full'
    for value in workers.values():
        assert value['weather']['weather_loading_mode'] == 'full'
        assert value['weather']['full_field_hours_loaded'] == len(data.frames)


def test_cancel_stops_running_processes_within_two_seconds():
    def wind(hour,u,v):
        u[:,3] = 12
    search = search_for(dataset(wind), request())
    cancel, ready, outcome = Event(), Event(), []
    def compute():
        try:
            with planning_scope(monotonic()+30, cancel, progress=lambda _: ready.set()):
                search.search()
        except PlanningCancelled:
            outcome.append('cancelled')
    thread = Thread(target=compute)
    thread.start()
    assert ready.wait(15)
    began = monotonic()
    cancel.set()
    thread.join(2)
    assert not thread.is_alive() and outcome == ['cancelled']
    assert monotonic()-began < 2


@pytest.mark.parametrize('algorithm', ['astar','lpa_star','wa_lpa_star'])
def test_parallel_gfs_uses_selected_speed_source_and_hard_wind(algorithm):
    from app.flight_replay import build_profile
    from app.models import FlightProfileRequest
    def wind(hour,u,v):
        u[1:4,3] = 12
    data = dataset(wind)
    data = replace(data, source='gfs', dataset_id='gfs-parallel-test',
                   frames=tuple((stamp,replace(grid,source='GFS synthetic')) for stamp,grid in data.frames))
    req = request(planner_type=algorithm).model_copy(update={'source':'gfs','average_ground_speed_mps':50})
    with planning_scope(monotonic()+20,Event()):
        result = search_for(data,req).search(algorithm)
    flight = build_profile(dict(route_id='gfs-test',name='gfs-test',points=result['points'],source='gfs',level=req.level),
        FlightProfileRequest(source='gfs',departure_at=req.departure_at,average_ground_speed_mps=50,wind_shear=req.wind_shear),data)
    assert flight['decision']['status']=='allowed'
    assert flight['source']=='gfs' and flight['average_ground_speed_mps']==50
    assert any(abs(p[1])>.15 for p in result['points'])


@pytest.mark.parametrize('algorithm',['astar','lpa_star','wa_lpa_star'])
def test_parallel_candidate_refinement_cannot_hide_a_risky_corner(algorithm):
    from test_grid_coverage import small_data, req, profile
    from app.grid_coverage import cell_visits
    data,request=small_data(6.5),req(planner_type=algorithm)
    weather=LocalWeather(data,request,build_strategy_cost_config(request.planning_strategy))
    search=ParallelPlanner(weather,request.start,request.end,[0,0,.2,.2])
    with planning_scope(monotonic()+20,Event()):
        result=search.search(algorithm)
    assert result['objective_cost']<weather.route_cost([request.start,request.end])
    assert result['objective_cost']==pytest.approx(weather.route_cost(result['points']))
    assert all((v.row,v.col)!=(2,1) for v in cell_visits(result['points'],weather.grid,10))
    assert profile(data,result['points'])['decision']['status']=='allowed'


@pytest.mark.parametrize('algorithm',['astar','lpa_star','wa_lpa_star'])
def test_cached_route_is_revalidated_at_its_new_actual_passage_times(monkeypatch,algorithm):
    monkeypatch.setenv('ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS','1')
    def wind(hour,u,v):
        if hour>=1: u[2,3]=12
    data=dataset(wind)
    req=request(planner_type=algorithm,seed_routes=[[[0,0],[.6,0]]])
    assert profile(data,{'points':[req.start,req.end]},req)['decision']['status']=='blocked'
    with planning_scope(monotonic()+15,Event()):
        result=search_for(data,req).search(algorithm)
    assert profile(data,result,req)['decision']['status']=='allowed'
    assert result['points']!=[req.start,req.end]
