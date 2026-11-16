from dataclasses import replace
from datetime import datetime,timedelta
from threading import Event
from time import monotonic

import numpy as np
import pytest

from app import wrf_timeline as timeline
from app.departure_windows import _vector_issues,prepare_evaluation,describe_evaluation
from app.flight_replay import build_profile
from app.local_weather import LocalWeather
from app.models import FlightProfileRequest
from app.parallel_routing import ParallelPlanner
from app.planning_control import planning_scope
from app.route_options import build_strategy_cost_config
from app.wind_provider import WindGrid
from app.wrf_native_schema import NATIVE_WRF_TIME_MODEL as NATIVE
from test_temporal_routing import BASE,request


def native_dataset(transform=None,minutes=range(0,181,10)):
    frames=[]
    for minute in minutes:
        u,v=np.ones((5,7)),np.zeros((5,7))
        if transform:transform(minute,u,v)
        valid=BASE+timedelta(minutes=minute)
        grid=WindGrid(np.linspace(0,.06,7),np.linspace(.02,-.02,5),u,v,
            BASE.strftime('%Y-%m-%d %H:%M UTC'),minute/60,'100m AGL',valid.strftime('%Y-%m-%d %H:%M UTC'),
            source='WRF native fixture',forecast_minutes=minute,wind_time_model=NATIVE)
        frames.append((valid.timestamp(),grid))
    return timeline.ForecastDataset('native-'+str(id(frames)),frames[0][1].cycle,'100m AGL',tuple(frames),
        BASE.timestamp(),(BASE+timedelta(hours=3)).timestamp())


@pytest.mark.parametrize('seconds,expected',[(0,2),(180,2),(599.999,2),(600,8),(601,8)])
def test_native_uses_exact_previous_frame_without_time_averaging(seconds,expected):
    data=native_dataset(lambda minute,u,v:u.fill(2 if minute==0 else 8))
    vector=timeline.vector_at(data,BASE.timestamp()+seconds,.02,0)
    assert vector==(expected,0)
    frame=timeline.wind_frame(data,BASE.timestamp()+seconds)
    assert set(frame['velocity'][0]['data'])=={expected}
    assert frame['metadata']['wind_time_model']==NATIVE


def test_missing_frame_is_not_filled_even_with_safe_neighbouring_hours():
    data=native_dataset(minutes=[0,20,30,60,180])
    assert timeline.vector_at(data,BASE.timestamp()+599,.02,0)==(1,0)
    assert timeline.vector_at(data,BASE.timestamp()+600,.02,0) is None
    assert timeline.vector_at(data,BASE.timestamp()+1199,.02,0) is None
    assert timeline.vector_at(data,BASE.timestamp()+1200,.02,0)==(1,0)
    assert timeline.vector_at(data,data.coverage_end+1,.02,0) is None
    missing,unsafe=_vector_issues([0,3600],[(2,0),(2,0)],7.9,NATIVE)
    assert not unsafe and any(i.contains(900) for i in missing)


def test_empty_native_evaluation_keeps_native_identity_and_unknown_status():
    evaluation=prepare_evaluation([(.01,0),(.011,0)],[],10,7.9,None,wind_time_model=NATIVE)
    description=describe_evaluation(evaluation,BASE,10)
    assert description['wind_time_model']==NATIVE
    assert description['current_departure']['status']=='unknown'
    assert not description['departure_windows']


def test_historical_profile_reports_native_product_absence():
    data=replace(native_dataset(),frames=(),error='该历史批次没有原生 10 分钟 WRF 产物，不使用小时数据插值')
    profile=build_profile({'points':[(.01,0),(.011,0)],'source':'wrf','level':data.level,'route_id':'old','name':'old'},
        FlightProfileRequest(departure_at=BASE),data)
    assert profile['decision']['status']=='unknown'
    assert '没有原生 10 分钟' in profile['decision']['reason']
    assert profile['wind_time_model']==NATIVE and not profile['frame_times']


def test_safe_interval_ends_cannot_hide_a_dangerous_native_frame():
    data=native_dataset(lambda minute,u,v:u.fill(10 if minute==10 else 1))
    points=[(.01,0),(.011,0)]
    with planning_scope(monotonic()+3,Event()):
        evaluation=prepare_evaluation(points,list(data.frames),10,7.9,None,source='wrf')
        description=describe_evaluation(evaluation,BASE,10)
    assert description['wind_time_model']==NATIVE
    for row in description['departure_windows']:
        a,b=(datetime.fromisoformat(row[key].replace('Z','+00:00')).timestamp() for key in ('start','end'))
        assert not a<=BASE.timestamp()+650<=b
    req=FlightProfileRequest(departure_at=BASE+timedelta(seconds=595),average_ground_speed_mps=10,wind_shear={'enabled':False})
    result=build_profile({'points':points,'level':'100m AGL','source':'wrf','route_id':'native','name':'native'},req,data)
    assert result['decision']['status']=='blocked'


@pytest.mark.parametrize('algorithm',['astar','lpa_star','wa_lpa_star'])
def test_all_three_algorithms_plan_against_actual_native_frames(monkeypatch,algorithm):
    monkeypatch.setenv('ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS','1')
    def wind(minute,u,v):
        if minute>=10:u[2,3]=12
    data=native_dataset(wind)
    req=request(planner_type=algorithm).model_copy(update={'end':(.06,0),'departure_at':BASE+timedelta(minutes=5)})
    weather=LocalWeather(data,req,build_strategy_cost_config(req.planning_strategy))
    with planning_scope(monotonic()+15,Event()):
        result=ParallelPlanner(weather,req.start,req.end,[0,-.02,.06,.02]).search(algorithm)
    assert weather.route_cost(result['points'])<float('inf')
    assert any(abs(p[1])>.005 for p in result['points'])
    assert result['weather']['wind_time_model']==NATIVE
