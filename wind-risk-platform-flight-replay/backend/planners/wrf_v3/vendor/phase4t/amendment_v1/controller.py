"""Geometric local LOS + known-local-wind inversion, ZOH heading-rate control."""
import math,time,copy
import numpy as np
from phase3a_implementation.domain import VA,R0
from .model import (wrap,wind,velocity,at_goal,goal_errors,simulated_step,StructuralStop)

# Preregistered SINGLE configuration; no gain sweep or T2 tuning permitted.
CONFIG=dict(version='LOS_LOCAL_KNOWN_WIND_V1',tick_s=.02,lookahead_m=15.,
    heading_gain_per_s=1.5,projection_window_parameter_s=.1,projection_iterations=5,
    reference_method='RK45_DENSE_NO_RESET',plant_method='RK45_ZOH',
    final_behavior='HOLD_FINAL_TANGENT_NO_REPLAN_UNTIL_T_EXEC',
    terminal_position_over_R0=1e-6,terminal_heading_rad=1e-7,
    cross_track_role='METRIC_ONLY',adaptive_estimator=False)

def wind_inverse(direction,w):
    d=np.array(direction,float)
    if d.shape!=(2,) or not np.isfinite(d).all() or abs(np.linalg.norm(d)-1.)>1e-10:
        raise StructuralStop('INVALID_DESIRED_DIRECTION')
    n=np.array([-d[1],d[0]]);parallel=float(w@d);perp=float(w@n)
    discriminant=VA**2-perp**2
    if discriminant<0:raise StructuralStop('WIND_INVERSE_DISCRIMINANT_IN_DOMAIN')
    speed=parallel+math.sqrt(discriminant)
    if speed<=0:raise StructuralStop('WIND_INVERSE_NONPOSITIVE_GROUND_SPEED')
    air=speed*d-w
    return math.atan2(air[1],air[0]),dict(discriminant=discriminant,positive_ground_speed=speed)

def command(reference,i,s,q,w):
    r,v,d=reference.geometry(i,s)
    n=np.array([-d[1],d[0]]);cross=float(n@(q[:2]-r[:2]))
    course=math.atan2(d[1],d[0])-math.atan2(cross,CONFIG['lookahead_m'])
    desired,proof=wind_inverse([math.cos(course),math.sin(course)],w)
    ff=reference.tr['controls'][i] if s<reference.ends[-1] else 0.
    raw=float(ff+CONFIG['heading_gain_per_s']*wrap(desired-q[2]))
    u=float(np.clip(raw,-VA/R0,VA/R0))
    actual_course=math.atan2(velocity(q,w)[1],velocity(q,w)[0])
    return u,dict(cross_track_signed_m=cross,desired_ground_course=course,
        ground_course=actual_course,psi_air=float(q[2]),
        sideslip_angle=wrap(actual_course-q[2]),wind_ratio=float(np.linalg.norm(w)/VA),
        raw_omega=raw,saturated=abs(raw)>=VA/R0,feedforward=ff,**proof)

def execute(route,provider,reference,clock,arm,*,initial_state=None):
    route.verify();tr=route.tr;q=np.array(tr['state_start'] if initial_state is None else initial_state,float)
    t=0.;i=0;s=0.;us=[];ds=[];nodes=[q.tolist()];history=[];compute=0.
    success=False
    if arm=='OPEN_LOOP':
        for u,d in zip(tr['controls'],tr['durations']):
            clock.check();q=simulated_step(q,float(u),float(d),provider,tr['z_layer_msl_m'],t)
            us.append(float(u));ds.append(float(d));t=math.fsum(ds);nodes.append(q.tolist())
        success=at_goal(q,tr['state_goal'])
    elif arm=='CLOSED_LOOP':
        while t<route.T_exec:
            clock.check()
            # Ordered local curve parameter, not the execution time. Only advance
            # into the next segment at the current segment endpoint.
            if s>=reference.ends[i]-1e-11 and i<len(reference.ends)-1:
                s=float(reference.ends[i]);i+=1
            s=reference.project(i,q,s,CONFIG['projection_window_parameter_s'])
            started=time.perf_counter();w=wind(provider,q,tr['z_layer_msl_m'],t)
            u,diag=command(reference,i,s,q,w);compute+=time.perf_counter()-started
            _,rv,_=reference.geometry(i,s)
            rate=max(.1,float(np.dot(velocity(q,w),rv)/np.dot(rv,rv)))
            remaining=(reference.ends[i]-s)/rate
            d=min(CONFIG['tick_s'],route.T_exec-t)
            if remaining>1e-11:d=min(d,float(remaining))
            if d<=1e-12:raise StructuralStop('ZERO_CONTROLLER_STEP')
            history.append(dict(t=t,path_parameter_s=s,active_segment=i,omega=u,**diag))
            q=simulated_step(q,u,d,provider,tr['z_layer_msl_m'],t)
            us.append(u);ds.append(d);t=math.fsum(ds);nodes.append(q.tolist())
            # Predictor uses actual longitudinal progress. Projection next tick
            # corrects it using geometry; s never skips to a different loop.
            s=float(min(reference.ends[i],s+rate*d))
            # Completion requires all original segments, not an earlier intersection.
            if i==len(reference.ends)-1 and s>=reference.ends[-1]-1e-10 and at_goal(q,tr['state_goal']):
                success=True;break
    else:raise StructuralStop('UNKNOWN_ARM')
    ex=copy.deepcopy(tr);ex['state_start']=nodes[0];ex['controls']=us;ex['durations']=ds;ex['states']=nodes
    return dict(arm=arm,planned_route_sha256=route.sha256,T_ref=route.T_ref,T_exec=route.T_exec,
        trajectory=ex,controller_history=history,controller_completed=success,
        physical_time_s=t,controller_compute_s=compute,
        preliminary_status='CONTROLLER_REPORTED_COMPLETE' if success else 'EXECUTION_TIMEOUT',
        original_goal_unchanged=ex['state_goal']==tr['state_goal'])
