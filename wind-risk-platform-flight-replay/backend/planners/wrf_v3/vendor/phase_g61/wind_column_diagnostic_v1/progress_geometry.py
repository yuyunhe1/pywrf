"""Bounded exact-input geometry reuse inside one independent progress assay.

The original six projection updates and every predicate still execute. Only
identical (segment, binary64 time) pure reference geometry calls are reused.
Every cache request checks deadline and object identity. All state/control
integrations remain independent and no memo persists between assessments.
"""
from collections import OrderedDict
from .work import content
from phase4t.amendment_v1.model import StructuralStop
from phase4t_g61.wrf_feasibility_validation_v1.runtime import ordered

ORIGINAL_PROGRESS=ordered.progress
POLICY=dict(version='EXACT_PROGRESS_GEOMETRY_MEMO_MAX32_PER_ASSESSMENT_V1',
    key='ORIGINAL_SEGMENT_AND_BINARY64_TIME_HEX',max_entries=32,
    lifetime='ONE_PROGRESS_ASSESSMENT',original_six_projection_updates_retained=True,
    all_original_predicates_and_sample_times_retained=True,
    physical_or_independent_ODE_query_caching=False)

class GeometryMemo:
    def __init__(self,reference,clock):
        self.reference=reference;self.clock=clock;self.cache=OrderedDict()
        self.identity=(id(reference),id(reference.provider),id(reference.tr))
        self.route_sha=content(reference.tr)
        self.requests=0;self.computations=0;self.hits=0

    def __getattr__(self,name):return getattr(self.reference,name)

    def bound(self):
        self.clock.check()
        if (id(self.reference),id(self.reference.provider),id(self.reference.tr))!=self.identity:
            raise StructuralStop('PROGRESS_MEMO_REFERENCE_SUBSTITUTION')

    def geometry(self,i,t):
        self.bound();self.requests+=1
        key=(int(i),float(t).hex())
        if key in self.cache:
            self.hits+=1;value=self.cache.pop(key);self.cache[key]=value
            return value
        value=self.reference.geometry(i,t)
        for array in value:array.setflags(write=False)
        self.computations+=1;self.cache[key]=value
        if len(self.cache)>32:self.cache.popitem(last=False)
        return value

    def finish(self):
        self.bound()
        if content(self.reference.tr)!=self.route_sha:
            raise StructuralStop('PROGRESS_MEMO_ROUTE_MUTATED')
        return dict(requests=self.requests,geometry_computations=self.computations,
            exact_input_cache_hits=self.hits,entries=len(self.cache),max_entries=32)

def progress(actual,reference,times,position_uncertainty,heading_uncertainty,*,clock,statistics=None):
    memo=GeometryMemo(reference,clock)
    result=ORIGINAL_PROGRESS(actual,memo,times,position_uncertainty,heading_uncertainty)
    counts=memo.finish()
    if statistics is not None:statistics.update(counts)
    return result
