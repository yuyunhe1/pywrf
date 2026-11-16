"""Diagnostic adapter for independent spatial C; never propagates using A."""
import copy
import math
from .validator import validate


def evaluate_spatial(trajectory_2d, wind_provider, *, z_layer_msl_m):
    enriched = copy.deepcopy(trajectory_2d)
    enriched['z_layer_msl_m'] = z_layer_msl_m
    try:
        controls = enriched['controls']
        time = math.fsum(enriched['durations'])
    except (KeyError,TypeError,ValueError) as exc:
        return {'valid':False,'time':None,'trajectory':None,'failures':['INVALID_INPUT'],'detail':str(exc)}
    return validate(enriched,controls,time,wind_provider)
