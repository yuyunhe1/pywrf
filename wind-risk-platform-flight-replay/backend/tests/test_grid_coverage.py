"""An independent rectangle-clipping oracle and risk at measure-zero contacts."""
from dataclasses import replace
import hashlib
import math
from threading import Event
from time import monotonic

import numpy as np
import pytest

from app.grid_coverage import cell_visits, boundary_contacts, SPATIAL_RISK_MODEL
from app.departure_windows import Interval, prepare_evaluation, describe_evaluation
from app.flight_replay import build_profile
from app.local_weather import LocalWeather
from app.models import FlightProfileRequest
from app.planning_control import planning_scope
from app.route_options import build_strategy_cost_config
from app.route_refinement import RouteRefiner
from app.route_service import haversine_km
from app.segmented_routing import SegmentedPlanner
from app.temporal_routing import TemporalWeather
from test_temporal_routing import BASE, dataset, request


def small_data(value=1., side=(2,1), transform=None):
    data=dataset(hours=range(6))
    frames=[]
    for hour,(stamp,grid) in enumerate(data.frames):
        u,v=np.ones((3,3)),np.zeros((3,3))
        u[side]=value
        if transform:
            transform(hour,u,v)
        frames.append((stamp,replace(grid,lons=np.array([0.,.1,.2]),lats=np.array([.2,.1,0.]),u=u,v=v)))
    identity=hashlib.sha256(b''.join(grid.u.tobytes()+grid.v.tobytes() for _,grid in frames)).hexdigest()[:16]
    return replace(data,dataset_id='closed-cells-'+identity,frames=tuple(frames))


def req(**overrides):
    return request(planning_strategy='wind_avoidance').model_copy(update={'start':(0.,0.),'end':(.2,.2),**overrides})


def weather(data,route=None,kind='full'):
    route=route or req()
    config=build_strategy_cost_config(route.planning_strategy)
    return TemporalWeather(data,route,config) if kind=='temporal' else LocalWeather(data,route,config,loading_mode=kind)


def profile(data,points,route=None):
    route=route or req()
    return build_profile(dict(route_id='isolated',name='corner',level=route.level,points=points,source=data.source),
        FlightProfileRequest(departure_at=route.departure_at,average_ground_speed_mps=route.average_ground_speed_mps,
                             source=data.source,wind_shear=route.wind_shear),data)


def intersects(a,b,box):
    lo,hi=0.,1.
    for dimension in (0,1):
        delta=b[dimension]-a[dimension]
        low,high=box[dimension],box[dimension+2]
        if not delta:
            if not low-1e-12 <= a[dimension] <= high+1e-12:
                return False
        else:
            x,y=sorted(((low-a[dimension])/delta,(high-a[dimension])/delta))
            lo,hi=max(lo,x),min(hi,y)
    return lo<=hi+1e-12


@pytest.mark.parametrize('flip_lon,flip_lat',[(False,False),(True,False),(False,True),(True,True)])
def test_supercover_matches_closed_rectangle_clipping_in_all_axis_orders(flip_lon,flip_lat):
    grid=small_data().frames[0][1]
    if flip_lon: grid=replace(grid,lons=grid.lons[::-1])
    if flip_lat: grid=replace(grid,lats=grid.lats[::-1])
    rng=np.random.default_rng(42)
    segments=[((0.,0.),(.2,.2)),((0.,.2),(.2,0.)),((.05,0.),(.05,.2)),
              ((0.,.05),(.2,.05)),((0.,0.),(.05,.05)),((.05,.05),(.2,.2))]
    segments += [(tuple(a),tuple(b)) for a,b in rng.uniform(0,.2,(60,2,2))]
    for a,b in segments:
        expected=set()
        for row,lat in enumerate(grid.lats):
            for col,lon in enumerate(grid.lons):
                box=(max(0,lon-.05),max(0,lat-.05),min(.2,lon+.05),min(.2,lat+.05))
                if intersects(a,b,box): expected.add((row,col))
        actual={(v.row,v.col) for v in cell_visits([a,b],grid,10)}
        assert actual==expected,(a,b,actual,expected)


@pytest.mark.parametrize('kind',['full','local','temporal'])
@pytest.mark.parametrize('side',[(2,1),(1,0),(1,2),(0,1)])
def test_dangerous_corner_only_contact_blocks_in_both_travel_directions(kind,side):
    data=small_data(9.,side)
    w=weather(data,kind=kind)
    for points in [[req().start,req().end],[req().end,req().start]]:
        edge=w.edge(*points)
        assert any((v.row,v.col)==side and v.enter==v.leave for v in edge.visits)
        assert not edge.allowed(BASE.timestamp())
        flight=profile(data,points)
        assert flight['decision']['status']=='blocked'
        assert flight['analysis']['max_wind_speed']==9
        assert any(s['status']=='blocked' and s['wind_speed']==9 for s in flight['analysis']['samples'])


@pytest.mark.parametrize('kind',['full','local','temporal'])
def test_risky_point_gets_nonzero_soft_cost_and_splitting_cannot_remove_it(kind):
    calm=weather(small_data(),kind=kind)
    risky=weather(small_data(6.5),kind=kind)
    points=[req().start,req().end]
    assert risky.edge(*points).allowed(BASE.timestamp())
    assert risky.route_cost(points)>calm.route_cost(points)+100
    split=[points[0],(.05,.05),(.15,.15),points[1]]
    assert risky.route_cost(split)==pytest.approx(risky.route_cost(points),rel=2e-5)
    contacts=boundary_contacts(points,risky.grid,10)
    assert contacts and all(c.exposure_km>0 for c in contacts)


@pytest.mark.parametrize('kind',['full','local','temporal'])
def test_following_boundary_uses_both_sides_and_endpoint_touch_is_not_free(kind):
    data=small_data(9.,(1,1))
    w=weather(data,kind=kind)
    assert not w.edge((.05,0.),(.05,.2)).allowed(BASE.timestamp())
    assert not w.edge((0.,0.),(.05,.05)).allowed(BASE.timestamp())
    assert profile(data,[(.05,0.),(.05,.2)])['decision']['status']=='blocked'


def test_missing_side_cell_is_unknown_and_all_corner_shear_pairs_are_checked():
    missing=small_data(np.nan)
    flight=profile(missing,[req().start,req().end])
    assert flight['decision']['status']=='unknown'
    assert any(s['wind_speed'] is None for s in flight['analysis']['samples'])
    data=small_data(-5.,transform=lambda h,u,v: u.__setitem__((1,1),5.))
    route=req(wind_shear=FlightProfileRequest(departure_at=BASE).wind_shear)
    w=weather(data,route)
    assert not w.edge(route.start,route.end).allowed(BASE.timestamp())
    flight=profile(data,[route.start,route.end],route)
    assert flight['decision']['status']=='blocked'
    assert flight['analysis']['wind_shear']['max_horizontal_wind_shear']==10
    assert any(event['type']=='shear' for event in flight['events'])


def test_corner_hazard_time_is_shifted_by_actual_passage_and_windows_do_not_bridge_it():
    data=small_data(transform=lambda hour,u,v: u.__setitem__((2,1),10. if hour==1 else 1.))
    w=weather(data)
    edge=w.edge(req().start,req().end)
    domain=Interval(BASE.timestamp(),BASE.timestamp()+3*3600)
    safe=w.safe_departures(edge,domain)
    check=prepare_evaluation([req().start,req().end],list(data.frames),10,7.9,None)
    statuses=set()
    for offset in range(0,3*3600+1,113):
        stamp=BASE.timestamp()+offset
        allowed=not any(issue.departure.contains(stamp) for issue in check.issues)
        statuses.add(allowed)
        assert edge.allowed(stamp)==allowed
        assert any(interval.contains(stamp) for interval in safe)==allowed
    assert statuses=={False,True}
    result=describe_evaluation(check,BASE,10)
    assert result['spatial_risk_model']==SPATIAL_RISK_MODEL
    assert len(result['departure_windows'])>=2


@pytest.mark.parametrize('algorithm',['astar','lpa_star','wa_lpa_star'])
def test_search_avoids_risky_flyable_corner_and_final_shortcuts_keep_it_avoided(algorithm):
    data=small_data(6.5)
    route=req(planner_type=algorithm)
    w=weather(data,route)
    search=SegmentedPlanner(w,route.start,route.end,[0,0,.2,.2])
    with planning_scope(monotonic()+15,Event()):
        result=search.search(algorithm)
    assert result['objective_cost']<w.route_cost([route.start,route.end])
    points,cost,_=RouteRefiner(w,result['points']).finish()
    assert cost<=result['objective_cost']+1e-8
    assert all((v.row,v.col)!=(2,1) for v in cell_visits(points,w.grid,10))
    assert profile(data,points)['decision']['status']=='allowed'


def test_corner_contact_uses_the_frame_at_actual_passage_including_exact_frame_boundary():
    data=small_data(transform=lambda hour,u,v: u.__setitem__((2,1),8. if hour==0 else 0.))
    speed=haversine_km(req().start,req().end)*1000/2400
    route=req(average_ground_speed_mps=speed)
    w=weather(data,route)
    edge=w.edge(route.start,route.end)
    assert any(v.enter==pytest.approx(600) and (v.row,v.col)==(2,1) for v in edge.visits)
    assert edge.allowed(BASE.timestamp())
    assert profile(data,[route.start,route.end],route)['decision']['status']=='allowed'
    faster=req(average_ground_speed_mps=speed*2400/2399.96)
    early=weather(data,faster)
    assert not early.edge(faster.start,faster.end).allowed(BASE.timestamp())
    assert profile(data,[faster.start,faster.end],faster)['decision']['status']=='blocked'


@pytest.mark.parametrize('kind',['full','local','temporal'])
def test_gfs_corner_contact_blocks_at_fifty_metres_per_second(kind):
    data=replace(small_data(9.),source='gfs')
    data=replace(data,frames=tuple((stamp,replace(grid,source='GFS')) for stamp,grid in data.frames))
    route=req(source='gfs',average_ground_speed_mps=50)
    w=weather(data,route,kind)
    assert not w.edge(route.start,route.end).allowed(BASE.timestamp())
    flight=profile(data,[route.start,route.end],route)
    assert flight['decision']['status']=='blocked' and flight['source']=='gfs'
    assert flight['analysis']['max_wind_speed']==9
