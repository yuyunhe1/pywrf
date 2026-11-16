from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import math
from threading import Barrier, Event
from time import monotonic, sleep

import pytest

from app import recommendations as module
from app.recommendations import Workflow, RecommendationJobManager, gains
from app.models import RecommendationJobRequest, FlightProfileRequest
from app.planning_control import planning_scope, checkpoint
from app.planning_control import PlanningTimedOut
from app.route_context import geometry_route
from app.flight_replay import build_profile
from app.local_weather import LocalWeather
from app.route_options import build_strategy_cost_config
from app.direction_pool import shutdown_direction_pool
from app.parallel_routing import ParallelPlanner
from test_temporal_routing import BASE, dataset, request


def job_request(**changes):
    values=request().model_dump()
    values.update(changes)
    return RecommendationJobRequest(**values)


def plan(req,data,points=None):
    route=geometry_route(points or [req.start,req.end],req.level,source=req.source)
    flight=build_profile(route,FlightProfileRequest(departure_at=req.departure_at,
        average_ground_speed_mps=req.average_ground_speed_mps,source=req.source,wind_shear=req.wind_shear),data)
    return dict(points=route['points'],analysis=flight['analysis'],flight_profile=flight,route_context=route,
        distance_km=flight['analysis']['total_distance_km'],departure_at=flight['departure_at'],arrival_at=flight['arrival_at'],
        source=req.source,level=req.level,dataset_id=data.dataset_id,objective_cost=100.,planner_type=req.planner_type,
        average_ground_speed_mps=req.average_ground_speed_mps)


def engine(req,data,updates,published):
    state={}
    def report(value):
        state.update(value)
        updates.append(dict(state))
    return Workflow(req,data,report,published.append,now=lambda:BASE)


def test_sixty_second_initial_policy_releases_attempt_and_runs_hour_offsets_together(monkeypatch):
    monkeypatch.setenv('ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS','.2')
    barrier=Barrier(2); times=[]
    def compute(req,data,bbox,cells,publish,candidate_validator=None):
        offset=(req.departure_at-BASE).total_seconds()
        times.append(offset)
        if offset==0:
            while True: checkpoint(); sleep(.002)
        barrier.wait(timeout=3)
        publish(plan(req,data))
        checkpoint()
    monkeypatch.setattr(module,'compute_temporal_plan',compute)
    data=dataset();updates=[];published=[]
    with planning_scope(monotonic()+5,Event()):
        work=engine(job_request(),data,updates,published)
        work.run()
    assert times==[0,3600,7200]
    assert not published  # Future cards never become an official route.
    assert updates[-1]['recommendations']
    assert work.attempts[0]['status']=='unconfirmed'
    assert all(row['status']=='found' for row in work.attempts[1:])


def test_first_solution_removes_initial_limit_and_keeps_optimizing(monkeypatch):
    monkeypatch.setenv('ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS','.2')
    def compute(req,data,bbox,cells,publish,candidate_validator=None):
        value=plan(req,data);publish(value)
        sleep(.25);checkpoint()
        return value
    monkeypatch.setattr(module,'compute_temporal_plan',compute)
    updates=[];published=[]
    with planning_scope(monotonic()+3,Event()):
        work=engine(job_request(),dataset(),updates,published)
        assert work.run()['analysis']['navigation_allowed']
    assert len(work.attempts)==1 and published
    assert updates[-1]['requested_status']=='allowed'


def test_detour_is_bounded_keeps_anchors_and_checks_delayed_suffix():
    def wind(hour,u,v):
        u[1,:]=.5
        u[:,6]=8 if hour==2 else 1
    data=dataset(wind)
    points=[(0.,0.),(.1,0.),(.5,0.),(.6,0.)]
    req=job_request(mode='fixed_route',points=points,segment_index=1,max_detour_ratio=.5)
    full=LocalWeather(data,req,build_strategy_cost_config(req.planning_strategy))
    with planning_scope(monotonic()+5,Event()):
        work=engine(req,data,[],[])
        segment_points=[(.1,0.),(.2,.1),(.4,.1),(.5,0.)]
        segment_req=req.model_copy(update={'start':points[1],'end':points[2],
            'departure_at':BASE+timedelta(seconds=full.edge(points[0],points[1]).duration)})
        segment=plan(segment_req,data,segment_points)
        assert segment['analysis']['navigation_allowed']
        assert full.route_cost(points)<math.inf
        assert work._full_detour(segment,BASE,full) is None
        safe=dataset(lambda hour,u,v: u.__setitem__((1,slice(None)),.5))
        safe_full=LocalWeather(safe,req,build_strategy_cost_config(req.planning_strategy))
        accepted=work.__class__(req,safe,lambda x:None,lambda x:None,now=lambda:BASE)._full_detour(segment,BASE,safe_full)
        assert accepted['points'][0]==points[0] and accepted['points'][-1]==points[-1]
        assert tuple(accepted['points'][1])==points[1] and tuple(accepted['points'][-2])==points[-2]
        too_long={**segment,'points':[(.1,0.),(.1,.2),(.5,.2),(.5,0.)]}
        assert work._full_detour(too_long,BASE,full) is None


def test_tiny_same_cell_detour_finishes_after_baseline_without_searching(monkeypatch):
    req=job_request(mode='fixed_route',points=[(.099,0),(.101,0)],segment_index=0,max_detour_ratio=.5)
    updates=[]
    with planning_scope(monotonic()+5,Event()):
        work=engine(req,dataset(),updates,[])
        monkeypatch.setattr(work,'attempt',lambda *a,**kw:pytest.fail('same-cell search must be skipped'))
        work.run()
    assert updates[-1]['stage']=='completed' and updates[-1]['detour_search_skipped']
    assert updates[-1]['baseline_windows']['departure_windows']
    assert not updates[-1]['recommendations']


def test_window_gains_measure_union_difference_and_unknown_never_claims_benefit():
    def ranges(*pairs):
        return {'coverage_incomplete':False,'departure_windows':[{'start':(BASE+timedelta(minutes=a)).isoformat(),
            'end':(BASE+timedelta(minutes=b)).isoformat()} for a,b in pairs]}
    before,after=ranges((60,90),(120,150)),ranges((30,90),(135,180))
    value=gains(before,after)
    assert value['earlier_minutes']==30
    assert value['gained_minutes']==60 and value['lost_minutes']==15 and value['net_minutes']==45
    assert value['improves']
    assert not gains({**before,'coverage_incomplete':True},after)['benefit_confirmed']


def test_owned_manager_completes_without_automatically_publishing_future_cards(monkeypatch):
    monkeypatch.setattr(module,'_current_utc',lambda:BASE)
    monkeypatch.setenv('ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS','.2')
    def compute(req,data,bbox,cells,publish,candidate_validator=None):
        if req.departure_at==BASE:
            while True: checkpoint(); sleep(.002)
        publish(plan(req,data));checkpoint()
    monkeypatch.setattr(module,'compute_temporal_plan',compute)
    manager=RecommendationJobManager(workers=1,timeout_seconds=5)
    try:
        state=manager.submit_workflow(job_request(),dataset())
        end=monotonic()+4
        while state['status'] not in {'completed','failed'}:
            assert monotonic()<end
            sleep(.01);state=manager.snapshot(state['job_id'])
        assert state['status']=='completed',state
        assert state['workflow']['recommendations'] and not state['result']
        assert manager.snapshot(state['job_id'],since_workflow_revision=state['workflow_revision'])['workflow'] is None
    finally: manager.shutdown()


def test_different_departures_use_four_distinct_processes_with_overlap(monkeypatch):
    monkeypatch.setenv('ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS','1')
    barrier=Barrier(2)
    data=dataset()
    def run(offset):
        req=request().model_copy(update={'departure_at':BASE+timedelta(minutes=offset)})
        weather=LocalWeather(data,req,build_strategy_cost_config(req.planning_strategy))
        barrier.wait(timeout=5)
        with planning_scope(monotonic()+20,Event()):
            return ParallelPlanner(weather,req.start,req.end,[-.1,-.3,.7,.3]).search()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b=list(pool.map(run,[0,10]))
        workers=[v['pid'] for result in [a,b] for v in result['direction_workers'].values()]
        assert len(set(workers))==4
        assert a['observed_parallel_overlap_seconds']>0 and b['observed_parallel_overlap_seconds']>0
    finally: shutdown_direction_pool()


def test_same_geometry_keeps_an_earlier_single_departure_certificate(monkeypatch):
    with planning_scope(monotonic()+5,Event()):
        work=engine(job_request(),dataset(),[],[])
        monkeypatch.setattr(work,'windows',lambda points: (_ for _ in ()).throw(PlanningTimedOut()))
        later=job_request(departure_at=BASE+timedelta(hours=2))
        earlier=job_request(departure_at=BASE+timedelta(hours=1))
        work.add_candidate(plan(later,work.dataset))
        work.add_candidate(plan(earlier,work.dataset))
    assert len(work.cards)==1
    card=next(iter(work.cards.values()))
    assert module.stamp(card['first_departure_at'])==earlier.departure_at.timestamp()
    assert len(card['verified_departures'])==2 and not card['windows']['departure_windows']


@pytest.mark.parametrize('action',['cancel','owner_loss','forecast'])
def test_recommendation_parent_stops_both_future_attempts_and_clears_old_progress(monkeypatch,action):
    monkeypatch.setattr(module,'_current_utc',lambda:BASE)
    monkeypatch.setenv('ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS','5')
    ready=Barrier(3);stopped=[];valid=Event();valid.set()
    def compute(req,data,bbox,cells,publish,candidate_validator=None):
        if req.departure_at==BASE:
            return {'route_status':'no_feasible_route','planning_message':'起飞时刻的起点风速达到硬上限'}
        ready.wait(3)
        try:
            while True: checkpoint();sleep(.005)
        finally: stopped.append(req.departure_at)
    monkeypatch.setattr(module,'compute_temporal_plan',compute)
    manager=RecommendationJobManager(workers=1,timeout_seconds=10,owner_timeout_seconds=.15,monitor_interval=.005)
    try:
        state=manager.submit_workflow(job_request(client_instance_id='owned'),dataset(),revision_check=valid.is_set)
        job=manager.jobs[state['job_id']]
        ready.wait(3)
        with planning_scope(monotonic()+2,Event()):
            manager._report(job,{'search_progress':{'corridors':[]}})
            manager._report(job,{'search_progress':None})
        assert job.search_progress is None
        began=monotonic()
        if action=='cancel': manager.cancel_job(job.job_id)
        elif action=='forecast': valid.clear()
        while not job.future.done():
            assert monotonic()-began<2
            sleep(.01)
        state=manager.snapshot(job.job_id)
        assert state['status']=='cancelled' and len(stopped)==2
        if action=='forecast': assert state['workflow']['stage']=='forecast_changed'
    finally: manager.shutdown()


def test_expired_single_certificate_is_not_advertised_as_a_future_window(monkeypatch):
    monkeypatch.setattr(module,'_current_utc',lambda:BASE)
    manager=RecommendationJobManager(workers=1,timeout_seconds=2)
    try:
        monkeypatch.setattr(Workflow,'run',lambda self:None)
        state=manager.submit_workflow(job_request(),dataset())
        job=manager.jobs[state['job_id']]
        while not job.future.done(): sleep(.005)
        card={'id':'same','plan':{'distance_km':1},'first_departure_at':BASE.isoformat(),
            'verified_departure_at':BASE.isoformat(),'verified_departures':[BASE.isoformat()],
            'windows':{'departure_windows':[],'coverage_incomplete':True}}
        job.workflow={'recommendations':[card]}
        monkeypatch.setattr(module,'_current_utc',lambda:BASE+timedelta(minutes=1))
        assert not manager.snapshot(job.job_id)['workflow']['recommendations']
    finally: manager.shutdown()


def test_gfs_recommendation_creation_pins_its_own_source_and_default_speed(monkeypatch):
    from dataclasses import replace
    from app import main
    from fastapi.testclient import TestClient
    data=replace(dataset(),source='gfs')
    monkeypatch.setattr(main,'prepare_temporal_request',lambda req:(req,data))
    monkeypatch.setattr(main.gfs_provider,'discover_files',lambda:[])
    accepted={}
    def submit(req,pinned,*args,**kwargs):
        accepted.update(source=pinned.source,speed=req.average_ground_speed_mps)
        return {'job_id':'gfs-workflow','status':'queued'}
    monkeypatch.setattr(main.recommendation_jobs,'submit_workflow',submit)
    response=TestClient(main.app).post('/api/route/recommendation/jobs',json={
        'start':[0,0],'end':[.6,0],'source':'gfs','level':'100m AGL','cycle':'2026-11-03 00:00 UTC'})
    assert response.status_code==202,response.text
    assert accepted=={'source':'gfs','speed':50}
