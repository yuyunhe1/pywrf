"""Standalone time-aware LOS rate identity; no controller/planner integration.

Spatial gradient inputs retain the existing field-difference method. W_t is the
partial time derivative of the same wind model at the same fixed point. It is
not an extra term to add to an existing yaw-control feedforward.
"""
import math
import numpy as np
from phase3a_implementation.domain import VA,R0
from phase4t.amendment_v1.model import velocity,StructuralStop

def compute(ref,p,q,w,cfg,gradient_reference,gradient_actual,time_reference,time_actual):
    gr,g=np.asarray(gradient_reference,float),np.asarray(gradient_actual,float)
    tr,tq=np.asarray(time_reference,float),np.asarray(time_actual,float)
    if gr.shape!=(2,2) or g.shape!=(2,2) or tr.shape!=(2,) or tq.shape!=(2,) or not np.isfinite(np.r_[gr.ravel(),g.ravel(),tr,tq]).all():raise StructuralStop('TEMPORAL_RATE_DERIVATIVE_SCHEMA')
    i,s=p.i,p.s;r,vr,tau=ref.geometry(i,s);n=np.array([-tau[1],tau[0]])
    dq=q[:2]-r[:2];cross=float(n@dq);along=float(tau@dq);v=velocity(q,w);L=cfg['lookahead_m']
    aref=np.array([math.cos(r[2]),math.sin(r[2])]);uref=float(ref.tr['controls'][i])
    material_reference=gr@vr
    if np.any(tr):material_reference=material_reference+tr
    speed=float(np.linalg.norm(vr))
    chi_prime=float((VA*(vr@aref)*uref+vr[0]*material_reference[1]-vr[1]*material_reference[0])/(vr@vr))
    denom=speed-chi_prime*cross;status='INTERIOR_FRENET';fallback=None
    if p.complete:sdot=0.;status='COMPLETED_FIXED_TANGENT'
    elif denom<=cfg['projection_singularity_relative']*speed:sdot=0.;status='F0_FALLBACK_SINGULAR_OR_NONMINIMUM_PROJECTION';fallback=uref
    else:
        sdot=float(tau@v)/denom
        if s<=float(ref.starts[i])+1e-10 and sdot<0:sdot=0.;status='START_ACTIVE_CONSTRAINT_ONE_SIDED'
        if s>=float(ref.ends[i])-1e-10 and sdot>0:sdot=0.;status='END_ACTIVE_CONSTRAINT_ONE_SIDED'
    edot=float(n@v)-chi_prime*sdot*along
    chi=math.atan2(tau[1],tau[0])-math.atan2(cross,L)
    chidot=chi_prime*sdot-L/(L*L+cross*cross)*edot
    td=np.array([math.cos(chi),math.sin(chi)]);nd=np.array([-td[1],td[0]])
    b=float(nd@w)/VA;discr=1.-b*b
    if discr<=0:raise StructuralStop('DESIRED_RATE_INVERSE_DISCRIMINANT')
    root=math.sqrt(discr);material_actual=g@v
    if np.any(tq):material_actual=material_actual+tq
    ff=chidot*(1.+float(td@w)/(VA*root))-float(nd@material_actual)/(VA*root)
    if fallback is not None:ff=fallback
    if not math.isfinite(ff):raise StructuralStop('NONFINITE_DESIRED_RATE')
    return float(ff),dict(derivative_status=status,s_dot=sdot,course_rate_rad_s=chidot,
        reference_course_derivative_per_s=chi_prime,reference_time_derivative=tr.tolist(),actual_time_derivative=tq.tolist(),
        raw_rate_outside_physical_bound=abs(ff)>VA/R0,standalone_identity_only=True,controller_integrated=False)
