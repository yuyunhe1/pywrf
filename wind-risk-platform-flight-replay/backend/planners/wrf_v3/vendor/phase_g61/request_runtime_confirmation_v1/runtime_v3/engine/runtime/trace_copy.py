"""Exact specialized deepcopy for known wind receipts; all else uses stdlib.

No weather values, interpolation material, state or ODE result is cached.
"""
import copy as standard
from types import FunctionType,SimpleNamespace
from phase_g61.dynamic_wind import provider as original

SCALARS=(str,int,float,bool,type(None))
BASE=frozenset(('valid','status','kind','frame_id','height_datum','context_sha256','query','raw_query_repr','time_mode','sources','bracket','wx','wy','wz'))
SUFFIX=frozenset(('mission_context_sha256','suffix_view'))

def flat(value):return type(value) is dict and all(type(x) in SCALARS for x in value.values())
def recognized(value):
    if type(value) is not dict or value.get('valid') is not True:return False
    if frozenset(value) not in (BASE,BASE|SUFFIX):return False
    if any(type(v) not in SCALARS for k,v in value.items() if k not in ('query','sources','bracket','suffix_view')):return False
    if not flat(value['query']) or type(value['sources']) is not list or type(value['bracket']) is not list:return False
    if 'suffix_view' in value and not flat(value['suffix_view']):return False
    for source in value['sources']:
        if type(source) is not dict or 'source_indices' not in source or not flat(source['source_indices']):return False
        if any(type(v) not in SCALARS for k,v in source.items() if k!='source_indices'):return False
    return all(flat(x) for x in value['bracket'])

def shallow(value,memo):
    key=id(value)
    if key not in memo:memo[key]=value.copy()
    return memo[key]

def clone_record(value,memo):
    if id(value) in memo:return memo[id(value)]
    out=shallow(value,memo);out['query']=shallow(value['query'],memo)
    if id(value['sources']) not in memo:
        sources=[];memo[id(value['sources'])]=sources
        for item in value['sources']:
            cloned=shallow(item,memo);cloned['source_indices']=shallow(item['source_indices'],memo);sources.append(cloned)
    out['sources']=memo[id(value['sources'])]
    if id(value['bracket']) not in memo:
        memo[id(value['bracket'])]=[shallow(x,memo) for x in value['bracket']]
    out['bracket']=memo[id(value['bracket'])]
    if 'suffix_view' in value:out['suffix_view']=shallow(value['suffix_view'],memo)
    return out

def deepcopy(value,memo=None):
    memo={} if memo is None else memo
    if id(value) in memo:return memo[id(value)]
    if recognized(value):return clone_record(value,memo)
    if type(value) is list and all(recognized(x) for x in value):
        result=[];memo[id(value)]=result;result.extend(clone_record(x,memo) for x in value);return result
    return standard.deepcopy(value,memo)

def rebind(fn):
    scope=dict(fn.__globals__);scope['copy']=SimpleNamespace(deepcopy=deepcopy)
    result=FunctionType(fn.__code__,scope,fn.__name__,fn.__defaults__,fn.__closure__)
    result.__kwdefaults__=fn.__kwdefaults__;result.__qualname__=fn.__qualname__;result.__doc__=fn.__doc__
    return result

class FastTemporalWRF(original.TemporalWRF):
    wind_query=rebind(original.TemporalWRF.wind_query)
    __call__=wind_query
    trace=property(rebind(original.TemporalWRF.trace.fget))

class FastMissionSuffixView(original.MissionSuffixView):
    wind_query=rebind(original.MissionSuffixView.wind_query)
    __call__=wind_query
    trace=property(rebind(original.MissionSuffixView.trace.fget))
