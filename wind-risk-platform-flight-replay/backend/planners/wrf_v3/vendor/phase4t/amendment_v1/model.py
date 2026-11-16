"""Immutable route, explicit clocks and simulation reference, independent of A/B."""
import copy, math, time
from dataclasses import dataclass
import numpy as np
from scipy.integrate import solve_ivp
from phase3a_implementation.domain import VA, R0
from phase3a_implementation.coordinates import FRAME_ID, HEIGHT_DATUM
from phase3b.validator import THRESHOLDS
from phase4a_design.mock_contract import content_hash

class StructuralStop(RuntimeError): pass
class WallLimit(RuntimeError): pass
class DomainFail(RuntimeError): pass

def wrap(a): return math.atan2(math.sin(a),math.cos(a))

class Clock:
    def __init__(self, cap=120., now=time.perf_counter):
        self.now=now; self.start=now();self.cap=cap;self.deadline=self.start+cap
    def elapsed(self):return self.now()-self.start
    def check(self):
        if self.now()>=self.deadline:raise WallLimit('OFFLINE_PIPELINE_WALL_CAP')

class TimedProvider:
    """Delegation changes no query values, interpolation or coordinates."""
    def __init__(self, provider, clock):self.original=provider;self.clock=clock
    def __getattr__(self,k):return getattr(self.original,k)
    def wind_query(self,*a):
        self.clock.check();r=self.original.wind_query(*a);self.clock.check();return r

def wind(provider,q,z,t):
    s=provider.wind_query(float(q[0]),float(q[1]),float(z),float(max(0.,t)))
    if not s.get('valid'):raise DomainFail(s.get('status','WIND_QUERY_FAILED'))
    w=np.array([s[k] for k in ('wx','wy')],float)
    if not np.isfinite(w).all():raise StructuralStop('NONFINITE_WIND')
    if np.linalg.norm(w)/VA>=1.:raise DomainFail('CONNECTOR_OUT_OF_DOMAIN')
    return w

def velocity(q,w):return VA*np.array([math.cos(q[2]),math.sin(q[2])])+w

def goal_errors(q,g):return float(np.linalg.norm(np.array(q[:2])-g[:2])),abs(wrap(q[2]-g[2]))
def at_goal(q,g):
    p,h=goal_errors(q,g)
    return p/R0<=THRESHOLDS['position_over_R0'] and h<=THRESHOLDS['heading_rad']

@dataclass(frozen=True)
class PlannedRoute:
    canonical: str
    sha256: str
    @classmethod
    def from_trajectory(cls,tr):
        import json
        t=copy.deepcopy(tr)
        if t['parameters']!={'Va':VA,'R0':R0} or t['frame_id']!=FRAME_ID or t['height_datum']!=HEIGHT_DATUM:
            raise StructuralStop('ROUTE_FRAME_PARAMETERS_MISMATCH')
        if t['type']!='piecewise_constant' or t['control_interpolation']!='ZOH':raise StructuralStop('ROUTE_SCHEMA')
        us=np.array(t['controls']);ds=np.array(t['durations'])
        if ds.shape!=us.shape or us.ndim!=1 or len(us)==0 or not np.isfinite(np.r_[ds,us]).all() or np.any(ds<=0):
            raise StructuralStop('ROUTE_CONTROLS_DURATIONS_SCHEMA')
        if np.max(np.abs(us))/(VA/R0)>1.+THRESHOLDS['control_over_umax']:raise StructuralStop('CONTROL_BOUND')
        for k in ('state_start','state_goal'):
            if np.array(t[k]).shape!=(3,) or not np.isfinite(t[k]).all():raise StructuralStop('ROUTE_STATE_SCHEMA')
        return cls(json.dumps(t,sort_keys=True,separators=(',',':'),allow_nan=False),content_hash(t))
    @property
    def tr(self):
        import json
        return json.loads(self.canonical)
    @property
    def T_ref(self):return math.fsum(self.tr['durations'])
    @property
    def T_exec(self):return 2*self.T_ref+10.
    def verify(self):
        if content_hash(self.tr)!=self.sha256:raise StructuralStop('ROUTE_IDENTITY_MISMATCH')

class Reference:
    """Dense original ground geometry. No snapped nodes, A propagation, smoothing or loop deletion.

    Time is ONLY the immutable geometric curve parameter. It is never the execution
    clock. Reconstruct consecutively from q0 without resets at original state nodes.
    """
    def __init__(self,route,provider,clock):
        self.route=route;self.provider=provider;self.clock=clock
        self.tr=route.tr;self.z=self.tr['z_layer_msl_m'];self.ends=np.cumsum(self.tr['durations'])
        self.starts=np.r_[0.,self.ends[:-1]];self.sol=[]
        q=np.array(self.tr['state_start'],float)
        for a,b,u in zip(self.starts,self.ends,self.tr['controls']):
            def rhs(t,q):return np.r_[velocity(q,wind(provider,q,self.z,t)),u]
            s=solve_ivp(rhs,(float(a),float(b)),q,method='RK45',rtol=2e-12,
                atol=[R0*1e-13,R0*1e-13,1e-13],max_step=.05,dense_output=True)
            if not s.success:raise StructuralStop('REFERENCE_INTEGRATION_FAILURE')
            q=s.y[:,-1];self.sol.append(s.sol);clock.check()
        if not at_goal(q,self.tr['state_goal']):raise StructuralStop('REFERENCE_ORIGINAL_GOAL_MISMATCH')
    def q(self,i,s):return self.sol[i](float(np.clip(s,self.starts[i],self.ends[i])))
    def geometry(self,i,s):
        q=self.q(i,s);w=wind(self.provider,q,self.z,s);v=velocity(q,w)
        return q,v,v/np.linalg.norm(v)
    def project(self,i,q,guess,window):
        # Local ordered projection only; no self-intersection branch jumps.
        lo=max(self.starts[i],guess-window);hi=min(self.ends[i],guess+window)
        s=float(np.clip(guess,lo,hi))
        for _ in range(5):
            r,v,_=self.geometry(i,s)
            s=float(np.clip(s+np.dot(q[:2]-r[:2],v)/np.dot(v,v),lo,hi))
        return s

def simulated_step(q,u,d,provider,z,t):
    """Actual simulation plant RK45. C uses its own original DOP853 implementation."""
    if not math.isfinite(u) or abs(u)>VA/R0+1e-12:raise StructuralStop('CONTROL_BOUND')
    def rhs(t,q):return [*velocity(q,wind(provider,q,z,t)),u]
    s=solve_ivp(rhs,(t,t+d),q,method='RK45',rtol=2e-12,
        atol=[R0*1e-13,R0*1e-13,1e-13],max_step=min(.05,d))
    if not s.success or not np.isfinite(s.y).all():raise StructuralStop('EXECUTION_INTEGRATION_FAILURE')
    return s.y[:,-1]
