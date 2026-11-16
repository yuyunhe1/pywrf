from time import monotonic
from threading import Event

import numpy as np

from app.detour_filter import DetourWeatherFilter
from app.planning_control import planning_scope
from test_temporal_routing import dataset


def test_same_cell_wiggle_and_retrace_do_not_create_a_detour():
    data=dataset(lambda hour,u,v: u.__setitem__((2,1),2))
    with planning_scope(monotonic()+3,Event()):
        check=DetourWeatherFilter(data,[(.08,0),(.12,0)],10)
        assert check.redundant([(.08,0),(.09,.02),(.11,-.02),(.12,0)])
        assert check.redundant([(.08,0),(.12,0),(.09,0),(.12,0)])


def test_same_weather_in_other_cells_is_redundant_but_future_difference_is_not():
    original=[(.1,0),(.5,0)]
    detour=[(.1,0),(.2,.1),(.4,.1),(.5,0)]
    with planning_scope(monotonic()+3,Event()):
        assert DetourWeatherFilter(dataset(),original,10).redundant(detour)
        def future(hour,u,v):
            if hour==3: v[1,3]=.01
        assert not DetourWeatherFilter(dataset(future),original,10).redundant(detour)


def test_hazardous_corner_contact_is_preserved_and_its_removal_is_allowed():
    # The straight diagonal touches both side cells at the midpoint corner.
    def wind(hour,u,v): u[2,2]=9
    original=[(.1,0),(.2,.1)]
    unsafe_wiggle=[(.1,0),(.125,.025),(.15,.05),(.2,.1)]
    avoidance=[(.1,0),(.1,.1),(.2,.1)]
    with planning_scope(monotonic()+3,Event()):
        check=DetourWeatherFilter(dataset(wind),original,10)
        assert check.redundant(unsafe_wiggle)
        assert not check.redundant(avoidance)


def test_missing_data_is_never_assumed_equivalent():
    data=dataset(lambda hour,u,v: u.__setitem__((2,1),np.nan))
    with planning_scope(monotonic()+3,Event()):
        check=DetourWeatherFilter(data,[(.08,0),(.12,0)],10)
        assert not check.redundant([(.08,0),(.1,.02),(.12,0)])


def test_only_a_proven_same_cell_distance_cap_skips_search():
    with planning_scope(monotonic()+3,Event()):
        check=DetourWeatherFilter(dataset(),[(.099,0),(.101,0)],10)
        assert check.confined_to_one_cell(.4)
        assert not check.confined_to_one_cell(6)
        # Near a boundary, even close endpoints may reach a different cell.
        assert not DetourWeatherFilter(dataset(),[(.149,0),(.148,0)],10).confined_to_one_cell(.2)
