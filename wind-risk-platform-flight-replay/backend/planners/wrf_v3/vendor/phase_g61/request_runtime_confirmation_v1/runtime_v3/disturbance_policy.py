"""Owner constructs a private plant script from preregistered immutable inputs."""
import math


def private_input(mission,terminal):
    kind=mission['new_condition']
    if kind=='EARLY_CAUSAL_PREPARATION_PULSES':starts=[.5,1.6]
    elif kind=='TERMINAL_RELATIVE_DELAYED_PULSES':
        E=terminal['trigger_physical_s']
        if isinstance(E,bool) or not isinstance(E,(int,float)) or not math.isfinite(E) or E<=.1:raise ValueError('REGISTERED_DELAYED_CONDITION_NOT_ELIGIBLE')
        starts=[E-.1,E+1.]
    elif kind=='TWO_SEPARATE_FLIGHT_DEVIATIONS':starts=[.5,25.]
    elif kind=='TWO_SEPARATE_OBSTACLE_DEVIATIONS':starts=[.5,10.]
    else:raise ValueError('UNREGISTERED_PLANT_CONDITION')
    return dict(version='PRIVATE_FROZEN_ACTUATOR_PULSES_V1',faults=[dict(start_physical_s=t,duration_s=d) for t,d in zip(starts,[.1,.1] if kind in ('TWO_SEPARATE_FLIGHT_DEVIATIONS','TWO_SEPARATE_OBSTACLE_DEVIATIONS') else [.1,.05])])
