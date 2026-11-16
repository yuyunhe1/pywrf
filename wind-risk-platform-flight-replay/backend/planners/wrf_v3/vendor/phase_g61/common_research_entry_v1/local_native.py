"""Time-consistent local curve recovery. No terminal-success decision or planner."""
import math, time
from types import SimpleNamespace
import numpy as np
from scipy.integrate import solve_ivp
from phase3a_implementation.domain import VA, R0
from phase4t.amendment_v1.controller import wind_inverse
from phase4t.amendment_v1.model import StructuralStop, DomainFail, velocity, wrap
from phase4t.repair_v1.contract import CONFIG as ORIGINAL_CONFIG
from phase_g61.midflight_environment_coverage_v1.temporal_rate.kernel import compute as los_identity
from phase3b.validator import THRESHOLDS

UMAX = VA / R0
CONFIG = {**ORIGINAL_CONFIG, 'candidate':'TIME_CONSISTENT_CURVE_CAPTURE_V4',
    'gradient_difference_m':.25, 'projection_singularity_relative':.5,
    'cross_support_m':3., 'heading_support_rad':.75,
    'prediction_horizon_s':1.5, 'predictor_max_step_s':.05,
    'maintenance_cross_m':.1, 'maintenance_heading_rad':.005,
    'terminal_air_heading_immutable':True}

class Unsupported(RuntimeError):
    pass

def point(provider, q, z, t, *, physical=True):
    if not math.isfinite(t) or t < 0:
        raise DomainFail('OUT_OF_TIME_DOMAIN')
    out = provider.wind_query(float(q[0]), float(q[1]), float(z), float(t))
    if not out.get('valid'):
        raise DomainFail(out.get('status', 'WIND_QUERY_FAILED'))
    w = np.array([out['wx'], out['wy']], float)
    if not np.isfinite(w).all():
        raise StructuralStop('NONFINITE_WIND')
    if physical and np.linalg.norm(w) >= VA:
        raise DomainFail('CONNECTOR_OUT_OF_DOMAIN')
    return w

class Field:
    """Same scalar queries; no persistent wind/gradient cache."""
    def __init__(self, provider, z, interval=None):
        self.provider, self.z, self.interval = provider, float(z), interval
        self.queries = 0
        if provider.kind != 'SYNTHETIC_CONSTANT':
            if interval is None or len(interval)!=2 or not np.isfinite(interval).all() or not 0<=interval[0]<interval[1]:
                raise StructuralStop('EXPLICIT_NATIVE_TIME_INTERVAL_REQUIRED')
            from .native_clock import NativeTimeCells
            self.native_cells=NativeTimeCells(provider,interval)

    def value(self, q, t, *, physical=True):
        if self.interval is not None and not self.interval[0] <= t <= self.interval[1]:
            raise DomainFail('OUT_OF_TIME_DOMAIN')
        self.queries += 1
        return point(self.provider, q, self.z, t, physical=physical)

    def derivatives(self, q, t, cfg=CONFIG):
        if self.provider.kind == 'SYNTHETIC_CONSTANT':
            self.value(q,t)
            return np.zeros((2,2)), np.zeros(2)
        h = cfg['gradient_difference_m']
        columns = []
        # Preserve the old center/plus/minus query semantics and step.
        self.value(q,t)
        for j in (0,1):
            plus, minus = np.array(q,float), np.array(q,float)
            plus[j] += h; minus[j] -= h
            columns.append((self.value(plus,t)-self.value(minus,t))/(2*h))
        a,b = self.native_cells.bracket(t)
        wt = (self.value(q,b,physical=False)-self.value(q,a,physical=False))/(b-a)
        return np.column_stack(columns), wt

def check_state(q, t):
    if np.asarray(q).shape != (3,) or not np.isfinite(q).all():
        raise StructuralStop('RECOVERY_STATE_SCHEMA')
    if not math.isfinite(t) or t < 0:
        raise DomainFail('OUT_OF_TIME_DOMAIN')

def coordinates(ref, field, i, s, q, actual_t):
    check_state(q,actual_t)
    r, vr, tau = ref.geometry(i,s)
    w = field.value(q,actual_t)
    wrapped, _ = wind_inverse(tau,w)
    star = float(r[2])+wrap(wrapped-float(r[2]))
    normal = np.array([-tau[1],tau[0]])
    displacement = np.asarray(q[:2])-r[:2]
    return dict(e=float(normal@displacement), eta=float(q[2])-star,
        along=float(tau@displacement), star=star, r=r, vr=vr, tau=tau, w=w)

def frame_rate(ref, field, i, s, cfg=CONFIG):
    r,vr,tau = ref.geometry(i,s)
    gr,wrt = field.derivatives(r,s,cfg)
    material = gr@vr+wrt
    air = np.array([math.cos(r[2]),math.sin(r[2])])
    uref = float(ref.tr['controls'][i])
    chi = float((VA*(vr@air)*uref+vr[0]*material[1]-vr[1]*material[0])/(vr@vr))
    return r,vr,tau,chi

def jet(ref, field, i, s, q, actual_t, cfg=CONFIG):
    c = coordinates(ref,field,i,s,q,actual_t)
    r,vr,tau,chi = frame_rate(ref,field,i,s,cfg)
    speed = float(np.linalg.norm(vr)); denom = speed-chi*c['e']
    if denom <= cfg['projection_singularity_relative']*speed:
        raise Unsupported('FRENET_DENOMINATOR')
    v = velocity(q,c['w']); sdot = float(tau@v)/denom
    if sdot <= 0:
        raise Unsupported('NONFORWARD_PROJECTED_MOTION')
    g,wt = field.derivatives(q,actual_t,cfg)
    normal = np.array([-tau[1],tau[0]])
    root = math.sqrt(1.-float(normal@c['w'])**2/VA**2)
    rate = chi*sdot*(1.+float(tau@c['w'])/(VA*root))-float(normal@(g@v+wt))/(VA*root)
    return {**c, 'sdot':sdot, 'star_rate':rate,
        'bilateral_rate_margin':UMAX-abs(rate), 'reference_course_derivative':chi}

def project(ref, i, s, q):
    r,v,_ = ref.geometry(i,s)
    guess = s+float((q[:2]-r[:2])@v/(v@v))
    return ref.project(i,q,guess,.1)

def stats():
    return dict(surface_calls=0,prediction_ode_calls=0,prediction_nfev=0,prediction_inclusive_s=0.)

def stopping(ref, field, i, s, q, actual_t, statistics, cfg=CONFIG):
    statistics['surface_calls'] += 1
    c = coordinates(ref,field,i,s,q,actual_t)
    if abs(c['along'])>1e-6:
        return dict(valid=False,reason='PROJECTION_NOT_ORTHOGONAL')
    if abs(c['e'])>cfg['cross_support_m'] or abs(c['eta'])>cfg['heading_support_rad']:
        return dict(valid=False,reason='OUTSIDE_LOCAL_OR_LIFT_SUPPORT')
    if abs(c['eta'])<=cfg['capture_roundoff_rad']:
        return dict(valid=True,surface=c['e'],brake_time_s=0.,reason='DYNAMIC_HEADING_ALREADY_MATCHED')
    if s>=float(ref.ends[i])-1e-10:
        return dict(valid=False,reason='NO_ORIGINAL_SEGMENT_REMAINING')
    u = -math.copysign(UMAX,c['eta']); sign = math.copysign(1.,c['eta'])

    def rhs(t,y):
        ref.clock.check()
        qq,ss = y[:3], float(np.clip(y[3],ref.starts[i],ref.ends[i]))
        r,vr,tau,chi = frame_rate(ref,field,i,ss,cfg)
        normal = np.array([-tau[1],tau[0]])
        e = float(normal@(qq[:2]-r[:2])); speed=float(np.linalg.norm(vr))
        denom = speed-chi*e
        if denom<=cfg['projection_singularity_relative']*speed:
            raise Unsupported('FRENET_DENOMINATOR')
        v=velocity(qq,field.value(qq,t));sdot=float(tau@v)/denom
        if sdot<=0:raise Unsupported('NONFORWARD_PROJECTED_MOTION')
        return [*v,u,sdot]

    def matched(t,y):
        return coordinates(ref,field,i,float(y[3]),y[:3],t)['eta']
    matched.terminal=True;matched.direction=-sign
    def boundary(t,y):return float(ref.ends[i]-y[3])
    boundary.terminal=True;boundary.direction=-1
    start=time.perf_counter()
    try:
        statistics['prediction_ode_calls']+=1
        sol=solve_ivp(rhs,(actual_t,actual_t+cfg['prediction_horizon_s']),np.r_[q,s],
            method='DOP853',rtol=2e-12,atol=[R0*1e-13,R0*1e-13,1e-13,1e-13],
            max_step=cfg['predictor_max_step_s'],events=[matched,boundary])
        statistics['prediction_nfev']+=sol.nfev
    except Unsupported as exc:
        return dict(valid=False,reason=str(exc))
    finally:
        statistics['prediction_inclusive_s']+=time.perf_counter()-start
    if not sol.success or not np.isfinite(sol.y).all():
        raise StructuralStop('TEMPORAL_CAPTURE_PREDICTOR_FAILURE')
    if not len(sol.t_events[0]):
        return dict(valid=False,reason='BRAKE_LIMIT_OR_ORIGINAL_BRANCH_END')
    final=sol.y[:,-1];end=coordinates(ref,field,i,float(final[3]),final[:3],float(sol.t[-1]))
    return dict(valid=True,surface=end['e'],brake_time_s=float(sol.t[-1]-actual_t),brake_u=u,
        predicted_heading_residual_rad=end['eta'],predicted_along_residual_m=end['along'],
        prediction_actual_start_s=actual_t,prediction_actual_end_s=float(sol.t[-1]))

def los(ref, field, p, q, actual_t, cfg=CONFIG):
    c=coordinates(ref,field,p.i,p.s,q,actual_t)
    gr,wrt=field.derivatives(c['r'],p.s,cfg);g,wt=field.derivatives(q,actual_t,cfg)
    ff,diag=los_identity(ref,p,q,c['w'],cfg,gr,g,wrt,wt)
    chi=math.atan2(c['tau'][1],c['tau'][0])-math.atan2(c['e'],cfg['lookahead_m'])
    psi,_=wind_inverse([math.cos(chi),math.sin(chi)],c['w'])
    wanted=float(c['r'][2])+wrap(psi-float(c['r'][2]))
    raw=ff+cfg['heading_gain_per_s']*(wanted-float(q[2]))
    return float(np.clip(raw,-UMAX,UMAX)), dict(mode='TEMPORAL_LOS',raw_omega=raw,
        e=c['e'],eta=c['eta'],actual_t=actual_t,reference_s=p.s,desired_psi_air=wanted,
        derivative=diag),[]

def command(ref, field, p, q, *, actual_t, cfg=CONFIG):
    check_state(q,actual_t)
    if not hasattr(p,'temporal_stats'):p.temporal_stats=stats()
    if getattr(p,'temporal_branch',None)!=p.i:
        previous=getattr(p,'temporal_branch',None)
        maintain=(previous is not None and p.i==previous+1 and getattr(p,'temporal_phase','TRACK')=='FOLLOW_GEOMETRY')
        p.temporal_branch=p.i;p.temporal_phase='FOLLOW_GEOMETRY' if maintain else 'TRACK'
    # Preserve the original ZOH while the actual state at the actual clock is
    # numerically indistinguishable under the unchanged independent C bands.
    # This is a control deadband, never an exact-capture or success certificate.
    if not p.complete and ref.starts[p.i]-1e-12<=actual_t<=ref.ends[p.i]+1e-12:
        nominal=ref.q(p.i,actual_t)
        if np.linalg.norm(q[:2]-nominal[:2])<=R0*THRESHOLDS['position_over_R0'] and abs(float(q[2]-nominal[2]))<=THRESHOLDS['heading_rad']:
            return float(ref.tr['controls'][p.i]),dict(mode='NUMERICALLY_NOMINAL_ORIGINAL_ZOH',
                actual_t=actual_t,reference_s=p.s,not_a_capture_certificate=True),[]
    c=coordinates(ref,field,p.i,p.s,q,actual_t)
    if abs(c['eta'])>cfg['heading_support_rad']:
        return None,dict(mode='REPLAN_REQUIRED',reason='OUTSIDE_LOCAL_OR_LIFT_SUPPORT',eta=c['eta']),[]
    if p.complete:
        return None,dict(mode='REPLAN_REQUIRED',reason='IMMUTABLE_TERMINAL_BVP_REQUIRED'),[]
    if abs(c['e'])<=cfg['capture_roundoff_m'] and abs(c['eta'])<=cfg['capture_roundoff_rad'] and abs(c['along'])<=1e-8:
        p.temporal_phase='FOLLOW_GEOMETRY'
        u,d,ev=los(ref,field,p,q,actual_t,cfg)
        return u,{**d,'local_joint_roundoff_capture':True,'phase':'FOLLOW_GEOMETRY'},ev
    if abs(c['e'])<=cfg['capture_roundoff_m'] and abs(c['eta'])<=cfg['capture_roundoff_rad'] and abs(c['along'])<=1e-8 and abs(actual_t-p.s)<=1e-10:
        return float(ref.tr['controls'][p.i]),dict(mode='SYNCHRONIZED_ORIGINAL_ZOH',actual_t=actual_t,reference_s=p.s),[]
    if abs(c['e'])>cfg['cross_support_m']:
        return None,dict(mode='REPLAN_REQUIRED',reason='OUTSIDE_LOCAL_CAPTURE_SUPPORT'),[]
    if getattr(p,'temporal_phase','TRACK') in ('CHECK_CAPTURE','FOLLOW_GEOMETRY'):
        exact=abs(c['e'])<=cfg['capture_roundoff_m'] and abs(c['eta'])<=cfg['capture_roundoff_rad']
        if exact or (abs(c['e'])<=cfg['maintenance_cross_m'] and abs(c['eta'])<=cfg['maintenance_heading_rad']):
            p.temporal_phase='FOLLOW_GEOMETRY'
            u,d,ev=los(ref,field,p,q,actual_t,cfg)
            return u,{**d,'local_joint_roundoff_capture':exact,'phase':'FOLLOW_GEOMETRY'},ev
        # A later actual deviation leaves maintenance and starts a new feedback
        # attempt; it cannot inherit a permanent CHECK_CAPTURE latch.
        p.temporal_phase='TRACK'
    i,s=p.i,p.s;diag=dict(e=c['e'],eta=c['eta'],along=c['along'],actual_t=actual_t,reference_s=s)
    if p.temporal_phase=='BRAKE':
        u=p.temporal_brake_u;surface=None;reason='BRAKE_TO_DYNAMIC_TANGENT_HEADING'
    else:
        pred=stopping(ref,field,i,s,q,actual_t,p.temporal_stats,cfg)
        if not pred['valid']:
            u,d,ev=los(ref,field,p,q,actual_t,cfg)
            return u,{**d,'capture_support_rejection':pred['reason']},ev
        surface=pred['surface'];diag.update(surface_m=surface,predicted_brake_s=pred['brake_time_s'])
        if abs(surface)<=cfg['capture_roundoff_m']:
            p.temporal_phase='BRAKE';u=-math.copysign(UMAX,c['eta']);p.temporal_brake_u=u;reason='BRAKE_ON_STOPPING_SURFACE'
        else:
            u=-math.copysign(UMAX,surface);reason='APPROACH_STOPPING_SURFACE'
    events=[]
    if p.temporal_phase!='BRAKE':
        sign=math.copysign(1.,surface)
        def switch(t,qq):
            ss=project(ref,i,s,qq)
            pred=stopping(ref,field,i,ss,qq,t,p.temporal_stats,cfg)
            return pred['surface'] if pred['valid'] else sign*cfg['cross_support_m']
        switch.terminal=True;switch.direction=-sign;switch.g4_kind='TEMPORAL_STOPPING_SURFACE'
        events.append(switch)
    rate=jet(ref,field,i,s,q,actual_t,cfg)['star_rate']
    if c['eta']*(u-rate)<0 and abs(c['eta'])>cfg['capture_roundoff_rad']:
        sign=math.copysign(1.,c['eta'])
        def heading(t,qq):
            ss=project(ref,i,s,qq)
            return coordinates(ref,field,i,ss,qq,t)['eta']
        heading.terminal=True;heading.direction=-sign;heading.g4_kind='TEMPORAL_HEADING_MATCH'
        events.append(heading)
    return float(u),dict(mode='TEMPORAL_CURVE_CAPTURE',action=reason,**diag),events

def after_event(ref,field,p,q,actual_t,fired):
    if 'TEMPORAL_STOPPING_SURFACE' in fired:
        s=project(ref,p.i,p.s,q)
        eta=coordinates(ref,field,p.i,s,q,actual_t)['eta']
        p.temporal_phase='BRAKE';p.temporal_brake_u=-math.copysign(UMAX,eta)
    if 'TEMPORAL_HEADING_MATCH' in fired:
        s=project(ref,p.i,p.s,q)
        c=coordinates(ref,field,p.i,s,q,actual_t)
        if abs(c['e'])<=CONFIG['maintenance_cross_m'] and abs(c['eta'])<=CONFIG['maintenance_heading_rad']:
            # Geometric maintenance is sufficient here; do not continue a
            # saturated turn solely to force roundoff-level transverse capture.
            # The actual strict terminal is still a separate exact BVP contract.
            p.temporal_phase='FOLLOW_GEOMETRY'
        elif getattr(p,'temporal_phase','TRACK')=='BRAKE':
            p.temporal_phase='CHECK_CAPTURE'

def terminal_request(route, actual_state, actual_t, physical_deadline, trigger_perf, owner_deadline):
    """A request is not a candidate, acceptance receipt, or task success."""
    from .work import content
    check_state(actual_state,actual_t);route.verify()
    if not np.isfinite([physical_deadline,trigger_perf,owner_deadline]).all():
        raise StructuralStop('TERMINAL_REQUEST_CLOCK_SCHEMA')
    remaining=physical_deadline-actual_t
    if remaining<=8. or owner_deadline<trigger_perf+8.:
        return dict(status='REPLAN_BUDGET_INSUFFICIENT',strict_success=False)
    return dict(status='EXACT_TERMINAL_REQUEST_ONLY',actual_state=list(actual_state),actual_physical_s=actual_t,
        original_goal=route.tr['state_goal'],original_planned_route_sha256=route.sha256,
        original_goal_sha256=content(route.tr['state_goal']),remaining_global_physical_s=remaining,
        immutable_global_physical_deadline_s=physical_deadline,prepare_deadline_perf=trigger_perf+8.,
        owner_deadline_perf=owner_deadline,strict_success=False,new_route_found=False,
        original_path_recovery=False)
