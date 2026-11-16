"""Feedback stopping surface on the unchanged original curve.

Braking is a deterministic initial-value prediction to a *transverse heading
manifold*, not optimization to a goal/waypoint. No new reference, endpoint BVP,
state reset, control snap or planner. Local support only, explicitly guarded.
"""
import math,time
import numpy as np
from scipy.integrate import solve_ivp
from phase3a_implementation.domain import VA,R0
from phase4t.amendment_v1.model import wind,velocity,wrap,StructuralStop
from phase4t.recovery_g_20261020.desired_rate import gradient

UMAX=VA/R0

def coordinates(ref,i,s,q):
    r,v,tau=ref.geometry(i,s);n=np.array([-tau[1],tau[0]])
    return float(n@(q[:2]-r[:2])),wrap(float(q[2]-r[2])),float(tau@(q[:2]-r[:2]))

def projected(ref,i,s,q):
    r,v,_=ref.geometry(i,s)
    guess=s+float((q[:2]-r[:2])@v/(v@v))
    return ref.project(i,q,guess,.1)

def stopping(ref,i,s,q,cfg,stats):
    stats['surface_calls']+=1
    e,eta,along=coordinates(ref,i,s,q)
    if abs(along)>1e-6:return dict(valid=False,reason='PROJECTION_NOT_ORTHOGONAL')
    if abs(e)>cfg['g4_cross_support_m'] or abs(eta)>cfg['g4_heading_support_rad']:
        return dict(valid=False,reason='OUTSIDE_LOCAL_CAPTURE_SUPPORT')
    uref=float(ref.tr['controls'][i]);u=-math.copysign(UMAX,eta);sign=math.copysign(1.,eta)
    lift=round((float(q[2]-ref.q(i,s)[2])-eta)/(2*math.pi))
    if abs(uref)>cfg['g4_max_reference_control_fraction']*UMAX:
        return dict(valid=False,reason='REFERENCE_AUTHORITY_NOT_BILATERAL')
    if abs(eta)<=cfg['capture_roundoff_rad']:
        return dict(valid=True,surface=e,brake_time_s=0.,reason='HEADING_ALREADY_MATCHED')
    if s>=float(ref.ends[i])-1e-10:return dict(valid=False,reason='NO_ORIGINAL_SEGMENT_REMAINING')
    def rhs(t,y):
        ref.clock.check();qq=y[:3];ss=float(np.clip(y[3],ref.starts[i],ref.ends[i]));r,vr,tau=ref.geometry(i,ss)
        n=np.array([-tau[1],tau[0]]);wr=wind(ref.provider,r,ref.z,ss);w=wind(ref.provider,qq,ref.z,t)
        gr,_=gradient(ref.provider,r,ref.z,cfg['g4_gradient_step_m']);aref=np.array([math.cos(r[2]),math.sin(r[2])]);gv=gr@vr
        chi_prime=(VA*float(vr@aref)*uref+vr[0]*gv[1]-vr[1]*gv[0])/float(vr@vr)
        V=float(np.linalg.norm(vr));cross=float(n@(qq[:2]-r[:2]));denom=V-chi_prime*cross
        if denom<=cfg['g4_projection_min_denominator_fraction']*V:raise CaptureUnsupported('FRENET_DENOMINATOR')
        v=velocity(qq,w);sdot=float(tau@v)/denom
        if sdot<=0:raise CaptureUnsupported('NONFORWARD_PROJECTED_MOTION')
        return [*v,u,sdot]
    def matched(t,y):return float(y[2]-ref.q(i,y[3])[2]-lift*2*math.pi)
    matched.terminal=True;matched.direction=-sign
    def boundary(t,y):return float(ref.ends[i]-y[3])
    boundary.terminal=True;boundary.direction=-1
    began=time.perf_counter()
    try:
        stats['prediction_ode_calls']+=1
        sol=solve_ivp(rhs,(0.,cfg['g4_prediction_horizon_s']),np.r_[q,s],method='DOP853',
            rtol=2e-12,atol=[R0*1e-13,R0*1e-13,1e-13,1e-13],max_step=cfg['g4_predictor_max_step_s'],
            events=[matched,boundary])
        stats['prediction_nfev']+=sol.nfev
    except CaptureUnsupported as exc:
        return dict(valid=False,reason=str(exc))
    finally:stats['prediction_inclusive_s']+=time.perf_counter()-began
    if not sol.success:raise StructuralStop('G4_PREDICTOR_ODE_FAILURE')
    if not len(sol.t_events[0]):return dict(valid=False,reason='BRAKE_LIMIT_OR_ORIGINAL_BRANCH_END')
    final=sol.y[:,-1];stop_e,stop_eta,stop_along=coordinates(ref,i,float(final[3]),final[:3])
    return dict(valid=True,surface=stop_e,brake_time_s=float(sol.t[-1]),brake_u=u,
        predicted_heading_residual_rad=stop_eta,predicted_along_residual_m=stop_along)

class CaptureUnsupported(RuntimeError):pass

def command(ref,p,q,w,cfg,base):
    stats=getattr(p,'g4_stats',None)
    if stats is None:
        stats=dict(surface_calls=0,prediction_ode_calls=0,prediction_nfev=0,prediction_inclusive_s=0.)
        p.g4_stats=stats
    i,s=p.i,p.s;e,eta,along=coordinates(ref,i,s,q);uref=float(ref.tr['controls'][i])
    if getattr(p,'g4_branch',None)!=i:
        p.g4_branch=i;p.g4_phase='TRACK'
    diag=dict(g4_phase=p.g4_phase,g4_e_m=e,g4_eta_rad=eta,g4_along_m=along,reference_control=uref)
    if not p.complete and abs(e)<=cfg['capture_roundoff_m'] and abs(eta)<=cfg['capture_roundoff_rad'] and abs(along)<=1e-8:
        p.g4_phase='ON_REFERENCE'
        return uref,dict(mode='G4_ON_ORIGINAL_CURVE',**diag),[]
    if p.complete or abs(uref)>cfg['g4_max_reference_control_fraction']*UMAX:
        u,d,ev=base(ref,p,q,w,cfg);d.update(g4_support='COMPLETED_OR_SATURATED_REFERENCE',**diag);return u,d,ev
    if p.g4_phase=='CHECK_CAPTURE':
        # No hidden retries or weakened capture test after the actual brake event.
        u,d,ev=base(ref,p,q,w,cfg);d.update(g4_support='JOINT_CAPTURE_RESIDUAL_NOT_ZERO',**diag);return u,d,ev
    if p.g4_phase=='BRAKE':
        u=p.g4_brake_u;reason='BRAKE_TO_ORIGINAL_AIR_HEADING';surface=None
    else:
        prediction=stopping(ref,i,s,q,cfg,stats)
        if not prediction['valid']:
            u,d,ev=base(ref,p,q,w,cfg);d.update(g4_support=prediction['reason'],**diag);return u,d,ev
        surface=prediction['surface'];diag.update(g4_surface_m=surface,predicted_brake_s=prediction['brake_time_s'])
        if abs(surface)<=cfg['capture_roundoff_m']:
            p.g4_phase='BRAKE';u=-math.copysign(UMAX,eta);p.g4_brake_u=u;reason='BRAKE_ON_STOPPING_SURFACE'
        else:u=-math.copysign(UMAX,surface);reason='APPROACH_STOPPING_SURFACE'
    ev=[]
    if p.g4_phase!='BRAKE':
        sign=math.copysign(1.,surface)
        def switch(t,qq):
            ss=projected(ref,i,s,qq);pred=stopping(ref,i,ss,qq,cfg,stats)
            return pred['surface'] if pred['valid'] else sign*cfg['g4_cross_support_m']
        switch.terminal=True;switch.direction=-sign;switch.g4_kind='STOPPING_SURFACE'
        ev.append(switch)
    if eta*(u-uref)<0 and abs(eta)>cfg['capture_roundoff_rad']:
        sign_eta=math.copysign(1.,eta)
        def heading(t,qq):
            ss=projected(ref,i,s,qq);return coordinates(ref,i,ss,qq)[1]
        heading.terminal=True;heading.direction=-sign_eta;heading.g4_kind='HEADING_MATCH'
        ev.append(heading)
    return u,dict(mode='G4_CURVE_CAPTURE',g4_action=reason,**diag),ev

def after_event(ref,p,q,fired):
    if 'STOPPING_SURFACE' in fired:
        ss=projected(ref,p.i,p.s,q);eta=coordinates(ref,p.i,ss,q)[1]
        p.g4_phase='BRAKE';p.g4_brake_u=-math.copysign(UMAX,eta)
    if 'HEADING_MATCH' in fired and getattr(p,'g4_phase','TRACK')=='BRAKE':p.g4_phase='CHECK_CAPTURE'
