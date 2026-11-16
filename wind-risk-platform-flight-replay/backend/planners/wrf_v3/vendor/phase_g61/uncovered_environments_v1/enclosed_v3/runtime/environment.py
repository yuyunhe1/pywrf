"""Explicit time-aware context; original independent C/collision functions unchanged."""
import copy,datetime as dt,math
import numpy as np
from phase3a_implementation.coordinates import ROI
from phase3a_implementation.domain import VA,R0
from phase3a_implementation.synthetic import ConstantWindProvider
from phase3b.validator import validate,THRESHOLDS
from phase4b.collision import EdgeCollisionChecker,RectangleMask
from phase_g61.uncovered_environments_v1.shared_geometry import SharedGeometryTemporal as TemporalWRF,GuardedSuffix as MissionSuffixView
from phase_g61.online_execution_repair_v1.guarded_velocity import evaluation
from .common import ROOT,content,Timed,Stop

def load(case):
    spec=case['provider'];roi=ROI(**spec['roi'])
    if spec['kind']=='SYNTHETIC_CONSTANT':return ConstantWindProvider(*spec['wind'],roi=roi)
    if spec['kind']!='WRF_TIME_LINEAR_SNAPSHOTS':raise Stop('UNREGISTERED_PROVIDER_KIND')
    rows=copy.deepcopy(spec['records'])
    for r in rows:r['path']=str(ROOT/r['path'])
    return TemporalWRF.from_records(rows,roi=roi,epoch_utc=dt.datetime.fromisoformat(spec['epoch_utc']),
        max_gap_s=3600.,source_time_convention='UTC')

def bound(provider):
    if provider.kind=='SYNTHETIC_CONSTANT':b=math.hypot(*provider.wind[:2]);method='EXACT_CONSTANT_NORM'
    else:
        # Each original spatial/vertical interpolation and the temporal blend
        # is convex. Component extrema over both full native mass grids bound it.
        source=provider.provider if isinstance(provider,MissionSuffixView) else provider
        arrays=source._adapters
        if not all(np.isfinite(a.u).all() and np.isfinite(a.v).all() for a in arrays):raise Stop('MISSING_GLOBAL_SPEED_BOUND')
        b=math.hypot(max(float(np.max(np.abs(a.u))) for a in arrays),max(float(np.max(np.abs(a.v))) for a in arrays))
        method='TWO_NATIVE_FULL_GRID_COMPONENT_EXTREMA_CONVEX_TIME_SPACE_BOUND'
    return dict(max_ground_speed_mps=VA+b,wind_norm_bound_mps=b,method=method,
                source_sha256=provider.source['source_sha256'],implies_beta_domain=False)

class GuardedChecker(EdgeCollisionChecker):
    def check(self,trajectory):
        with evaluation(self.provider):return super().check(trajectory)

class Context:
    def __init__(self,case,raw,clock,offset=0.):
        self.case=case;self.raw=raw;self.clock=clock;self.offset=float(offset);self.z=case['route']['z_layer_msl_m']
        source=MissionSuffixView(raw,offset) if raw.kind=='WRF_TIME_LINEAR_SNAPSHOTS' else raw
        if source.kind=='SYNTHETIC_CONSTANT':
            self.provider=copy.copy(source)
            original=source.wind_query
            def checked(*args):
                clock.check();out=original(*args);clock.check();return out
            self.provider.wind_query=checked
        else:self.provider=Timed(source,clock)
        self.checker=GuardedChecker(ROI(**case['provider']['roi']),RectangleMask(case['rectangles']),
            provider=self.provider,speed_bound=bound(source),radius_m=case['radius_m'])
        self._descriptor=dict(version='G61_EXPLICIT_MISSION_CLOCK_CONTEXT_V1',source=source.source,
            mission_offset_s=offset,z_layer_msl_m=self.z,parameters=dict(Va=VA,R0=R0),case_sha256=content(case),
            collision=dict(mask_source=self.checker.mask.source,radius_m=self.checker.radius,ode_margin_m=self.checker.margin,speed_bound=self.checker.speed_bound),
            C=dict(thresholds=dict(THRESHOLDS)),provider_spec=copy.deepcopy(case['provider']))
        self.id=content(self._descriptor)
        self._objects=(id(self.raw),id(self.provider),id(self.checker),id(self.clock))
    @property
    def descriptor(self):return copy.deepcopy(self._descriptor)
    def assert_bound(self):
        if (id(self.raw),id(self.provider),id(self.checker),id(self.clock))!=self._objects:raise Stop('CONTEXT_OBJECT_SUBSTITUTION')
        if self.z!=self._descriptor['z_layer_msl_m'] or self.provider.kind!=self.case['provider']['kind']:raise Stop('CONTEXT_LAYER_OR_KIND_CHANGED')
        self.clock.check()
        if content(self._descriptor)!=self.id or content(self.case)!=self._descriptor['case_sha256'] or self.offset!=self._descriptor['mission_offset_s']:
            raise Stop('CONTEXT_MUTATED')
        if hasattr(self.raw,'assert_bound'):
            try:self.raw.assert_bound()
            except Exception as e:raise Stop('CONTEXT_SOURCE_BINDING:'+str(e)) from e
        if self.checker.provider is not self.provider:raise Stop('CHECKER_PROVIDER_SUBSTITUTED')
        live=dict(mask_source=self.checker.mask.source,radius_m=self.checker.radius,ode_margin_m=self.checker.margin,speed_bound=self.checker.speed_bound)
        if content(live)!=content(self._descriptor['collision']) or type(self.checker.mask) is not RectangleMask or self.checker.roi.as_dict()!=self.provider.roi.as_dict():raise Stop('COLLISION_CONTEXT_MUTATED')
        if content(self.provider.source)!=content(self._descriptor['source']) or THRESHOLDS!=self._descriptor['C']['thresholds']:raise Stop('SOURCE_OR_C_THRESHOLD_MUTATED')
    def evaluation(self):return evaluation(self.provider)
    def replay(self,tr,total):
        self.assert_bound()
        with self.evaluation():result=validate(tr,tr['controls'],total,self.provider)
        self.assert_bound();return result
    def at_offset(self,offset):return Context(self.case,self.raw,self.clock,offset)

def prefix(case):
    policy=case['prefix_policy'];us=list(policy['controls']);ds=list(policy['durations'])
    if policy['name']!='REFERENCE_SCHEDULE_8S' or len(us)!=len(ds) or abs(math.fsum(ds)-8.)>1e-12 or any(d<=0 for d in ds):raise Stop('FROZEN_PREFIX_SCHEMA')
    return us,ds
