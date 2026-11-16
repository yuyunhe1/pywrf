from threading import Event
from time import monotonic

import numpy as np
import pytest

from app.cost_evaluator import cost_config_from_thresholds
from app.local_weather import LocalWeather
from app.planning_control import planning_scope
from app.planning_objective import wind_preference
from app.route_options import build_strategy_cost_config
from app.temporal_plan_service import compute_temporal_plan
from app.wind_guidance import WindGuidance
from test_temporal_routing import dataset,request


@pytest.mark.parametrize('algorithm',['astar','lpa_star','wa_lpa_star'])
def test_distance_and_avoidance_choose_different_safe_routes_below_safe_label(algorithm):
    def wind(hour,u,v):
        u[:]=.4
        u[2,1:6]=1.4  # Both corridors are beneath the 1.5m/s display threshold.
    data=dataset(wind)
    results={}
    for strategy in ('distance_priority','wind_avoidance'):
        req=request(planner_type=algorithm,planning_strategy=strategy)
        with planning_scope(monotonic()+8,Event()):
            results[strategy]=compute_temporal_plan(req,data,[-.1,-.3,.7,.3],0)
    short,calm=results['distance_priority'],results['wind_avoidance']
    assert short['analysis']['navigation_allowed'] and calm['analysis']['navigation_allowed']
    assert len(short['points'])==2
    assert calm['analysis']['mean_wind_speed']<short['analysis']['mean_wind_speed']*.8
    assert calm['distance_km']>short['distance_km']
    assert calm['objective_model']=='SUPERCOVER_WIND_EXPOSURE_V5'


def test_avoidance_has_continuous_wind_preference_below_safe_without_changing_hard_limits():
    req=request()
    config=cost_config_from_thresholds(req.thresholds,build_strategy_cost_config('wind_avoidance'))
    def value(speed): return wind_preference(speed,speed,0,config.weights,config.thresholds)
    assert value(.3)<value(.8)<value(1.4)<value(5.5)<value(7.8)
    assert config.thresholds.max_wind_speed==7.9
    assert build_strategy_cost_config('distance_priority')['weights']['beta_wind']<1
    assert build_strategy_cost_config('wind_avoidance',legacy_v3=True)['weights']['beta_wind']==220
    assert 'epsilon_wind_exposure' not in build_strategy_cost_config('wind_avoidance',legacy_v3=True)['weights']


def test_wind_guidance_retains_finite_priorities_for_hazardous_and_missing_cells():
    def wind(hour,u,v):
        u[2,3]=12
        u[1,3]=np.nan
    data=dataset(wind)
    req=request()
    weather=LocalWeather(data,req,build_strategy_cost_config('wind_avoidance'))
    guide=WindGuidance(weather,req.start,range(5),range(7))
    values=guide.potential(req.end)
    assert len(values)==35 and all(np.isfinite(list(values.values())))
    assert np.isfinite(guide.density[2,3]) and np.isfinite(guide.density[1,3])
    assert not weather.edge((.2,0),(.4,0)).allowed(req.departure_at.timestamp())


def test_calibrated_weights_share_distance_scale_and_preserve_balanced_and_v3():
    expected = {'distance_priority': (.5, 0), 'balanced': (4, 4), 'wind_avoidance': (80, 128)}
    for strategy, (risk, exposure) in expected.items():
        weights = build_strategy_cost_config(strategy)['weights']
        assert weights['alpha_distance'] == 1
        assert (weights['beta_wind'], weights['epsilon_wind_exposure']) == (risk, exposure)
    assert build_strategy_cost_config('wind_avoidance', legacy_v3=True)['weights']['beta_wind'] == 220


@pytest.mark.parametrize('algorithm', ['astar', 'lpa_star', 'wa_lpa_star'])
def test_uniform_wind_can_use_the_same_straight_geometry_for_all_strategies(algorithm):
    data = dataset(lambda hour, u, v: u.fill(.4))
    for strategy in ('distance_priority', 'balanced', 'wind_avoidance'):
        req = request(planner_type=algorithm, planning_strategy=strategy)
        with planning_scope(monotonic() + 8, Event()):
            result = compute_temporal_plan(req, data, [-.1, -.3, .7, .3], 0)
        assert result['analysis']['navigation_allowed']
        assert [tuple(point) for point in result['points']] == [req.start, req.end]
