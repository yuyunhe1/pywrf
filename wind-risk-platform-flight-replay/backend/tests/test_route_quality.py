import json
import math
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

from app import temporal_plan_service as service
from app.local_weather import LocalWeather
from app.planning_control import checkpoint, planning_scope, PlanningCancelled, search_progress
from app.route_quality import assess_route_quality, simple_geometry_seed, MODEL
from app.route_refinement import RouteRefiner
from app.route_jobs import RouteJobManager
from test_temporal_routing import dataset, request
from test_route_refinement import ProbeWeather
from test_recommendations import plan, engine, job_request
from datetime import timedelta


@pytest.mark.parametrize('points', [
    [(0,0),(.03,0),(.01,.001),(.06,.03)],
    [(0,0),(.04,.04),(0,.04),(.04,0),(.08,0)],
    [(0,0),(.02,0),(.01,0),(.03,0)],
])
def test_real_retracing_or_crossings_are_not_displayable(points):
    assert not assess_route_quality(points)['acceptable']


def test_spiral_without_repeated_vertices_is_detected_but_broad_bypass_is_allowed():
    spiral=[((.025-i*.012/48)*math.cos(i*math.pi/12),(.025-i*.012/48)*math.sin(i*math.pi/12)) for i in range(49)]
    assert assess_route_quality(spiral)['local_circling_count']>0
    assert assess_route_quality([(0,0),(.04,.08),(.14,.08),(.2,0)])['acceptable']
    assert assess_route_quality([(0,0),(.001,.001),(.002,.0005),(.003,.0015)])['acceptable']
    assert assess_route_quality([(0,0),(.01,0),(.00999999999,0),(.02,0)])['acceptable']


def test_actual_latest_user_task_is_a_frozen_acceptance_case():
    case=json.loads((Path(__file__).parent/'fixtures/route_quality_user_20261115.json').read_text(encoding='utf-8'))
    assessment=assess_route_quality(case['points'],case['average_ground_speed_mps'])
    assert not assessment['acceptable']
    assert assessment['u_turn_count']==4
    assert assessment['cycle_count']>0 and assessment['local_circling_count']>0
    assert case['job_id']=='d8a004b7-ddc3-42f4-b486-5d742d47bb69'
    seed=simple_geometry_seed(case['points'],case['average_ground_speed_mps'])
    assert seed[0]==tuple(case['points'][0]) and seed[-1]==tuple(case['points'][-1])
    assert assess_route_quality(seed)['acceptable']


def test_safe_nonintersecting_reversal_can_be_removed_despite_higher_wind_cost():
    points=[(0,0),(.03,0),(.01,.001),(.06,.03)]
    weather=ProbeWeather(lambda p:10. if len(p)==4 else 20.)
    weather.speed=10
    result,cost,_=RouteRefiner(weather,points,prefer_simple=True).finish()
    assert assess_route_quality(result)['acceptable']
    assert cost==20


def install_probe(monkeypatch,search):
    loop=[(0.,0.),(.2,0.),(.1,0.),(.6,0.)]
    class IdentityRefiner:
        def __init__(self,weather,points,**kwargs): self.weather,self.points=weather,points
        def finish(self): return self.points,self.weather.route_cost(self.points),{}
    def weather(*args,**kwargs):
        value=LocalWeather(*args,**kwargs)
        value.route_cost=lambda points:1. if points==loop else 20.
        return value
    class ProbePlanner:
        def __init__(self,*a,**kw): pass
        def search(self,algorithm,accept): return search(accept,loop)
        def diagnostics(self): return {'expanded_nodes':1,'arrival_labels':1}
    monkeypatch.setattr(service,'LocalWeather',weather)
    monkeypatch.setattr(service,'RouteRefiner',IdentityRefiner)
    monkeypatch.setattr(service,'SegmentedPlanner',ProbePlanner)
    monkeypatch.setattr(service,'ParallelPlanner',ProbePlanner)


def candidate(points):
    return dict(points=points,planner_type='wa_lpa_star',expanded_nodes=1,arrival_labels=1,planning_time_ms=0)


def test_lower_wind_cost_cannot_publish_a_loop_and_simple_alternative_resolves_retry(monkeypatch):
    def search(accept,loop):
        assert accept(candidate(loop)) is None
        return accept(candidate([loop[0],loop[-1]]))
    install_probe(monkeypatch,search)
    events,published=[],[]
    req=request(prefer_simple_route=True)
    with planning_scope(monotonic()+5,Event(),progress=events.append):
        result=service.compute_temporal_plan(req,dataset(),[-.1,-.3,.7,.3],0,published.append)
    assert result['display_quality']['acceptable'] and result['analysis']['navigation_allowed']
    assert len(published)==1
    assert [e['route_quality']['status'] for e in events if 'route_quality' in e]==['searching_alternative','resolved']
    assert result['planning_diagnostics']['route_quality']['rejected_candidates']==1


def test_retry_expires_without_publishing_or_declaring_physical_impossibility(monkeypatch):
    def search(accept,loop):
        assert accept(candidate(loop)) is None
        while True: checkpoint();sleep(.001)
    install_probe(monkeypatch,search)
    req=request(prefer_simple_route=True,route_quality_extra_seconds=.05)
    published=[];began=monotonic()
    with planning_scope(monotonic()+5,Event()):
        result=service.compute_temporal_plan(req,dataset(),[-.1,-.3,.7,.3],0,published.append)
    assert .05<=monotonic()-began<1
    assert not published and result['points']==[]
    assert result['route_status']=='quality_unconfirmed'
    assert result['analysis']['navigation_status']=='unknown'
    assert result['planning_diagnostics']['attempts'][0]['stop_reason']=='route_quality_retry_exhausted'


def test_owner_cancellation_is_not_confused_with_extra_search_expiry(monkeypatch):
    cancel=Event()
    def search(accept,loop):
        accept(candidate(loop));cancel.set();checkpoint()
    install_probe(monkeypatch,search)
    with pytest.raises(PlanningCancelled),planning_scope(monotonic()+5,cancel):
        service.compute_temporal_plan(request(prefer_simple_route=True),dataset(),[-.1,-.3,.7,.3],0)


def test_display_finish_allows_the_remaining_quality_retry_then_releases():
    ready=Event()
    def compute(req,publish):
        publish(plan(req,dataset()))
        search_progress({'route_quality':dict(model=MODEL,status='searching_alternative',message='retry',
            retry_seconds=.06,retry_until_monotonic=monotonic()+.06)})
        ready.set()
        while True: checkpoint();sleep(.001)
    manager=RouteJobManager(workers=1,timeout_seconds=5)
    try:
        identity=manager.submit(compute,request(optimize_during_display=True))['job_id']
        assert ready.wait(2)
        state=manager.finish_job(identity)
        assert not state['finish_requested']
        deadline=monotonic()+1
        while not state['compute_finished']:
            assert monotonic()<deadline
            sleep(.005);state=manager.snapshot(identity)
        assert state['status']=='completed' and state['feasible']
    finally:manager.shutdown()


def test_quality_retry_can_extend_the_initial_attempt_limit_without_owner_cancellation(monkeypatch):
    from app import recommendations
    monkeypatch.setenv('ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS','.05')
    def compute(req,data,bbox,cells,publish,candidate_validator=None):
        sleep(.03)
        search_progress({'route_quality':dict(model=MODEL,status='searching_alternative',message='retry',
            retry_seconds=.1,retry_until_monotonic=monotonic()+.1)})
        sleep(.07);checkpoint()
        value=plan(req,data);publish(value);return value
    monkeypatch.setattr(recommendations,'compute_temporal_plan',compute)
    published=[]
    with planning_scope(monotonic()+3,Event()):
        work=engine(job_request(prefer_simple_route=True),dataset(),[],published)
        assert work.run()['analysis']['navigation_allowed']
    assert len(work.attempts)==1 and published


def test_uncertified_simple_geometry_is_only_guidance_and_is_rechecked_at_a_future_departure(monkeypatch):
    from app import recommendations
    from test_temporal_routing import BASE
    seed=[(0.,0.),(.3,.1),(.6,0.)]
    calls=[]
    def compute(req,data,bbox,cells,publish,candidate_validator=None):
        calls.append(req)
        if req.departure_at==BASE:
            return {'points':[],'route_status':'quality_unconfirmed','planning_diagnostics':{'route_quality':{'simple_seeds':[seed]}}}
        assert req.seed_routes==[seed]
        value=plan(req,data,seed)
        assert value['analysis']['navigation_allowed']
        publish(value);checkpoint();return value
    monkeypatch.setattr(recommendations,'compute_temporal_plan',compute)
    published=[]
    with planning_scope(monotonic()+5,Event()):
        work=engine(job_request(prefer_simple_route=True),dataset(hours=range(6)),[],published)
        assert work.attempt(BASE,initial=True) is None
        later=work.attempt(BASE+timedelta(hours=2))
    assert later['analysis']['navigation_allowed']
    assert len(calls)==2 and not published # Future schemes require explicit adoption.
