from datetime import timedelta
import math
from dataclasses import replace
from threading import Event, Barrier
from time import monotonic, sleep
from uuid import uuid4

import numpy as np
import pytest

from app import departure_range as module
from app.departure_range import DepartureRangeWorkflow
from app.local_weather import LocalWeather, preserves_waypoints
from app.models import RecommendationJobRequest, FlightProfileRequest
from app.planning_control import planning_scope
from app.range_evaluation import RouteRangeEvaluation, SELECTION_MODEL
from app.route_context import geometry_route
from app.route_options import build_strategy_cost_config
from app.flight_replay import build_profile
from app.temporal_plan_service import compute_temporal_plan
from app.wind_provider import WindGrid
from app.wrf_native_schema import NATIVE_WRF_TIME_MODEL
from app.wrf_timeline import ForecastDataset
from test_temporal_routing import BASE, request


def native(winds=None, count=19):
    frames = []
    for i in range(count):
        u, v = np.ones((5, 7)), np.zeros((5, 7))
        if winds: winds(i, u, v)
        grid = WindGrid(np.linspace(0, .06, 7), np.linspace(.02, -.02, 5), u, v,
            BASE.strftime('%Y-%m-%d %H:%M UTC'), i/6, '100m AGL',
            (BASE+timedelta(minutes=i*10)).strftime('%Y-%m-%d %H:%M UTC'),
            source='WRF', wind_time_model=NATIVE_WRF_TIME_MODEL)
        frames.append(((BASE+timedelta(minutes=i*10)).timestamp(), grid))
    return ForecastDataset('native-range-'+uuid4().hex, frames[0][1].cycle, '100m AGL', tuple(frames),
        frames[0][0], frames[-1][0])


def req(**changes):
    values = request().model_dump()
    values.update(start=(.001, 0.), end=(.059, 0.), mode='departure_range',
        departure_start_at=BASE, departure_end_at=BASE+timedelta(minutes=30))
    values.update(changes)
    return RecommendationJobRequest(**values)


def checked_plan(q, data, points=None):
    points = points or q.required_waypoints
    weather = LocalWeather(data, q, build_strategy_cost_config(q.planning_strategy), loading_mode='local')
    if not math.isfinite(weather.route_cost(points)):
        return {'route_status': 'unconfirmed'}
    route = geometry_route(points, q.level, source=q.source)
    profile = build_profile(route, FlightProfileRequest(departure_at=q.departure_at,
        average_ground_speed_mps=q.average_ground_speed_mps, thresholds=q.thresholds, wind_shear=q.wind_shear), data)
    return dict(points=points, analysis=profile['analysis'], flight_profile=profile, route_context=route,
        distance_km=profile['analysis']['total_distance_km'], departure_at=profile['departure_at'], arrival_at=profile['arrival_at'],
        max_contact_wind_speed_mps=weather.contact_peak(points), objective_cost=weather.route_cost(points),
        time_aware=True, source=q.source, level=q.level, planner_type=q.planner_type, dataset_id=data.dataset_id,
        average_ground_speed_mps=q.average_ground_speed_mps)


def test_range_validation_and_anchor_derivation():
    anchors = [(0., 0.), (.02, .01), (.04, 0.)]
    q = req(required_waypoints=anchors)
    assert q.start == anchors[0] and q.end == anchors[-1]
    for changes in [dict(departure_end_at=BASE-timedelta(minutes=1)),
                    dict(departure_start_at=BASE.replace(tzinfo=None)),
                    dict(departure_end_at=BASE+timedelta(seconds=1)),
                    dict(required_waypoints=[anchors[0], anchors[0]])]:
        with pytest.raises(ValueError): req(**changes)


def test_peak_cap_equality_and_danger_remain_distinct():
    data = native(lambda i, u, v: u.fill(2.))
    q = req().model_copy(update={'peak_wind_limit_mps': 2., 'minimize_distance_under_peak': True})
    w = LocalWeather(data, q, build_strategy_cost_config('wind_avoidance'), loading_mode='local')
    assert math.isfinite(w.route_cost([q.start, q.end]))
    assert w.cost(w.edge(q.start, q.end), BASE.timestamp()) == w.edge(q.start, q.end).distance_km
    exclusive = LocalWeather(data, q.model_copy(update={'peak_wind_limit_exclusive': True}), {}, loading_mode='local')
    assert math.isinf(exclusive.route_cost([q.start, q.end]))
    blocked = native(lambda i, u, v: u.fill(7.9))
    assert math.isinf(LocalWeather(blocked, q.model_copy(update={'peak_wind_limit_mps': 8.9}), {}, loading_mode='local').route_cost([q.start, q.end]))
    with planning_scope(monotonic()+2, Event()):
        result=compute_temporal_plan(q.model_copy(update={'peak_wind_limit_exclusive':True}),data,[-1,-1,1,1],0)
    assert result['route_status']=='peak_cap_blocked'
    assert not result['points'] and not result['planning_diagnostics']['attempts']


def test_envelope_matches_independent_contact_peak_at_every_minute_and_clips_windows():
    data = native(lambda i, u, v: u.fill(6. if i == 1 else 1.))
    q = req(start=(.001, 0.), end=(.009, 0.), average_ground_speed_mps=100.)
    material = RouteRangeEvaluation([q.start, q.end], data, q, BASE.timestamp(), (BASE+timedelta(minutes=30)).timestamp())
    w = LocalWeather(data, q, {}, loading_mode='local')
    for minute in range(31):
        at = (BASE+timedelta(minutes=minute)).timestamp()
        values = [peak for interval, peak in material.atoms if interval.contains(at)]
        assert max(values) == pytest.approx(w.contact_peak([q.start, q.end], at))
    windows = material.windows(2., BASE.timestamp())['departure_windows']
    assert windows
    for row in windows:
        a, b = module.epoch(row['start']), module.epoch(row['end'])
        assert BASE.timestamp() <= a <= b <= (BASE+timedelta(minutes=30)).timestamp()
        assert not a <= (BASE+timedelta(minutes=15)).timestamp() <= b
    assert len(windows) == 2  # Safe endpoints must not bridge the dangerous middle.


@pytest.mark.parametrize('algorithm', ['astar', 'lpa_star', 'wa_lpa_star'])
def test_layered_search_visits_exact_order_and_refinement_keeps_pins(monkeypatch, algorithm):
    monkeypatch.setenv('ROUTE_PLAN_DIRECTION_PROCESSES', '0')
    q = req(planner_type=algorithm, required_waypoints=[(.001,0.), (.025,.01), (.059,0.)])
    q = q.model_copy(update={'minimize_distance_under_peak': True, 'stop_on_first_feasible': True})
    with planning_scope(monotonic()+8, Event()):
        value = compute_temporal_plan(q, native(), [-1,-1,1,1], 0)
    assert value['analysis']['navigation_allowed'], value
    assert preserves_waypoints(value['points'], q.required_waypoints)
    assert value['max_contact_wind_speed_mps'] == 1.


def test_workflow_prefers_later_calm_departure_and_never_leaves_range(monkeypatch):
    data = native(lambda i, u, v: u.fill(5. if i < 2 else 1.))
    calls, reports, published = [], [], []
    def compute(q, data, bbox, cells, publish, **kw):
        calls.append((q.departure_at, q.peak_wind_limit_mps, q.minimize_distance_under_peak))
        value = checked_plan(q, data)
        if value.get('analysis', {}).get('navigation_allowed'): publish(value)
        return value
    monkeypatch.setattr(module, 'compute_temporal_plan', compute)
    with planning_scope(monotonic()+15, Event()):
        work = DepartureRangeWorkflow(req(), data, reports.append, published.append, now=lambda: BASE)
        result = work.run()
    assert result['selection_model'] == SELECTION_MODEL
    assert module.epoch(result['departure_at']) >= (BASE+timedelta(minutes=20)).timestamp()
    assert result['min_verified_peak_mps'] == 1.
    assert result['peak_wind_limit_mps'] == 2.
    assert all(BASE <= t <= BASE+timedelta(minutes=30) for t, _, _ in calls)
    assert any(cap is not None and distance for _, cap, distance in calls)
    assert published[0]['range_comparison_complete'] is False
    assert published[-1]['range_comparison_complete'] is True


def test_peak_band_exactly_one_chooses_shorter_and_lower_reference_invalidates_old():
    q, data, published, reports = req(), native(), [], []
    low = checked_plan(q, data, [q.start, (.03,.01), q.end])
    short = checked_plan(q, data, [q.start, q.end])
    low['max_contact_wind_speed_mps'] = 2.
    short['max_contact_wind_speed_mps'] = 3.
    with planning_scope(monotonic()+5, Event()):
        work = DepartureRangeWorkflow(q, data, reports.append, published.append, now=lambda: BASE)
        work.register(low); work.register(short)
        assert published[-1]['points'] == short['points']
        lower = {**low, 'departure_at': (BASE+timedelta(minutes=1)).isoformat(), 'max_contact_wind_speed_mps': 1.999}
        work.register(lower)
        assert published[-1]['points'] == low['points']
        assert published[-1]['max_contact_wind_speed_mps'] <= published[-1]['min_verified_peak_mps']+1.


def test_native_missing_frame_is_not_interpolated_or_advertised_as_a_window():
    data = native()
    data = replace(data, frames=tuple(row for i, row in enumerate(data.frames) if i != 1))
    q = req(start=(.001,0.), end=(.009,0.), average_ground_speed_mps=100.)
    value = RouteRangeEvaluation([q.start,q.end],data,q,BASE.timestamp(),(BASE+timedelta(minutes=30)).timestamp())
    at = (BASE+timedelta(minutes=15)).timestamp()
    assert not any(interval.contains(at) for interval,_ in value.atoms)
    result = value.windows(2.,BASE.timestamp())
    assert result['coverage_incomplete']
    assert not any(module.epoch(w['start'])<=at<=module.epoch(w['end']) for w in result['departure_windows'])


def test_peak_envelope_counts_a_high_wind_cell_touched_at_a_diagonal_corner():
    def winds(i,u,v): u[2,3]=7.5
    data=native(winds)
    q=req(start=(.02,0.),end=(.04,.02))
    w=LocalWeather(data,q,{},loading_mode='local')
    assert w.contact_peak([q.start,q.end])==7.5
    material=RouteRangeEvaluation([q.start,q.end],data,q,BASE.timestamp(),(BASE+timedelta(minutes=30)).timestamp())
    assert material.best_minute(BASE.timestamp())[0]==7.5
    assert not material.windows(7.4,BASE.timestamp())['departure_windows']


def test_parallel_workers_preserve_waypoint_phase_and_peak_cap(monkeypatch):
    from app.direction_pool import shutdown_direction_pool
    monkeypatch.setenv('ROUTE_PLAN_DIRECTION_PROCESSES','2')
    q=req(required_waypoints=[(.001,0.),(.025,.01),(.059,0.)]).model_copy(update={
        'minimize_distance_under_peak':True,'peak_wind_limit_mps':1.,'stop_on_first_feasible':True})
    try:
        with planning_scope(monotonic()+20,Event()):
            result=compute_temporal_plan(q,native(),[-1,-1,1,1],0)
        assert result['analysis']['navigation_allowed']
        assert preserves_waypoints(result['points'],q.required_waypoints)
        assert result['max_contact_wind_speed_mps']==1.
        stats=result['planning_diagnostics']['attempts'][0]
        assert len({w['pid'] for w in stats['direction_workers'].values()})==2
    finally:
        shutdown_direction_pool()


def test_parent_cancellation_stops_both_range_attempts(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from app.planning_control import checkpoint, PlanningCancelled
    barrier=Barrier(3); released=[]; cancel=Event()
    def compute(q,data,bbox,cells,publish):
        barrier.wait(5)
        try:
            while True: checkpoint();sleep(.002)
        finally: released.append(q.departure_at)
    monkeypatch.setattr(module,'compute_temporal_plan',compute)
    def run():
        with planning_scope(monotonic()+10,cancel):
            return DepartureRangeWorkflow(req(),native(),lambda x:None,lambda x:None,now=lambda:BASE).run()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(run)
        barrier.wait(5);began=monotonic();cancel.set()
        with pytest.raises(PlanningCancelled):future.result(timeout=2)
    assert len(released)==2 and monotonic()-began<2


def test_many_required_points_use_lazy_layers_and_shared_weather_domain():
    from app.ordered_waypoints import OrderedGraph
    anchors=[(.001+i*.0001, .001*(i%2)) for i in range(300)]
    q=req(required_waypoints=anchors)
    graph=OrderedGraph(LocalWeather(native(),q,{},loading_mode='local'),anchors,[-1,-1,1,1])
    assert len(graph.legs)==1
    graph.neighbours(('anchor',200))
    assert len(graph.legs)==2
    assert graph.legs[200].rows is graph.rows and graph.legs[200].cols is graph.cols
    assert graph.forward_bound(graph.start,anchors[0])==pytest.approx(sum(graph.lengths))


def test_future_range_does_not_relabel_its_start_as_depart_now():
    q=req()
    start=(BASE+timedelta(minutes=20)).timestamp()
    end=(BASE+timedelta(minutes=30)).timestamp()
    material=RouteRangeEvaluation([q.start,q.end],native(),q,start,end)
    windows=material.windows(2.,BASE.timestamp())
    assert module.epoch(windows['current_departure']['departure_at'])==BASE.timestamp()
    assert all(module.epoch(row['start'])>=start for row in windows['departure_windows'])


def test_manager_range_factory_retains_revisions_without_a_drawing_finish_deadline(monkeypatch):
    from app import recommendations
    from app.recommendations import RecommendationJobManager
    monkeypatch.setattr(module,'_current_utc',lambda:BASE)
    monkeypatch.setattr(recommendations,'_current_utc',lambda:BASE)
    def compute(q,data,bbox,cells,publish):
        value=checked_plan(q,data)
        if value.get('analysis',{}).get('navigation_allowed'):publish(value)
        return value
    monkeypatch.setattr(module,'compute_temporal_plan',compute)
    manager=RecommendationJobManager(workers=1,timeout_seconds=8)
    try:
        state=manager.submit_workflow(req(optimize_during_display=True),native())
        job=manager.jobs[state['job_id']]
        job.future.result(timeout=7)
        state=manager.snapshot(job.job_id)
        assert state['status']=='completed',state
        assert state['result']['mode']=='departure_range'
        assert state['result']['range_comparison_complete']
        assert job.display_segment_seconds is None and job.comparison_deadline is None
        assert state['workflow']['recommendations'][0]['plan']['max_contact_wind_speed_mps']==1.
        assert state['result']['selection_revision']>=2
    finally:manager.shutdown()


def test_recommendation_http_accepts_ordered_anchors_and_range_without_start_end(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, recommendations, route_storage
    from app.recommendations import RecommendationJobManager
    data=native()
    monkeypatch.setattr(module,'_current_utc',lambda:BASE)
    monkeypatch.setattr(recommendations,'_current_utc',lambda:BASE)
    monkeypatch.setattr(main,'prepare_temporal_request',lambda q:(q.model_copy(update={'dataset_id':data.dataset_id,'departure_at':BASE}),data))
    monkeypatch.setattr(main.wrf_cache_provider,'records_for_cycle',lambda cycle:[])
    def compute(q,data,bbox,cells,publish):
        value=checked_plan(q,data)
        if value.get('analysis',{}).get('navigation_allowed'):publish(value)
        return value
    monkeypatch.setattr(module,'compute_temporal_plan',compute)
    manager=RecommendationJobManager(workers=1,timeout_seconds=8)
    monkeypatch.setattr(main,'recommendation_jobs',manager)
    client=TestClient(main.app)
    try:
        payload=dict(mode='departure_range',source='wrf',level='100m AGL',required_waypoints=[(.001,0.),(.029,.01),(.059,0.)],
            departure_start_at=BASE.isoformat(),departure_end_at=(BASE+timedelta(minutes=10)).isoformat(),wind_shear={'enabled':False})
        response=client.post('/api/route/recommendation/jobs',json=payload)
        assert response.status_code==202,response.text
        job=manager.jobs[response.json()['job_id']]
        job.future.result(timeout=7)
        state=client.get('/api/route/recommendation/jobs/'+job.job_id).json()
        assert state['result']['selection_model']==SELECTION_MODEL
        assert preserves_waypoints(state['result']['points'],payload['required_waypoints'])
        assert not route_storage.list_routes()
        invalid=client.post('/api/route/recommendation/jobs',json={**payload,'departure_end_at':'2026-11-02T00:00:00Z'})
        assert invalid.status_code==422
    finally:manager.shutdown()


@pytest.mark.parametrize('algorithm',['astar','lpa_star','wa_lpa_star'])
def test_gfs_range_uses_its_existing_time_model_and_selected_speed(algorithm):
    from test_temporal_routing import dataset as hourly_dataset
    data=replace(hourly_dataset(),source='gfs')
    q=req(source='gfs',start=(.001,0.),end=(.02,0.),average_ground_speed_mps=50.,planner_type=algorithm)
    published=[]
    with planning_scope(monotonic()+8,Event()):
        work=DepartureRangeWorkflow(q,data,lambda value:None,published.append,now=lambda:BASE)
        value=work.run()
    assert value['source']=='gfs' and value['average_ground_speed_mps']==50.
    assert value['wind_time_model']==data.time_model
    assert value['max_contact_wind_speed_mps']==1.
    assert value['analysis']['navigation_allowed']
    assert value['departure_windows']['departure_windows']
