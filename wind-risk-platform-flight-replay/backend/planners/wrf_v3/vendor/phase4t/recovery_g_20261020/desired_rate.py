"""Causal LOS desired-air-heading rate. No next sample or recorded execution oracle.

Only replaces feedforward. The original field, approximate ordered projection,
and physical saturation remain. Frenet differentiation is exact only for a local
unique orthogonal projection. Residuals/fallbacks are exposed, not hidden.
"""
import math
import numpy as np
from phase3a_implementation.domain import VA,R0
from phase4t.amendment_v1.model import wind,velocity,StructuralStop

def gradient(provider,q,z,h):
    # Same frozen scalar-query provider and wind values; no smoothing or cache.
    if provider.kind=='SYNTHETIC_CONSTANT':return np.zeros((2,2)),0.
    cols=[]; asym=[]; w0=wind(provider,q,z,0.)
    for j in (0,1):
        plus=np.array(q,float);minus=plus.copy();plus[j]+=h;minus[j]-=h
        wp=wind(provider,plus,z,0.);wm=wind(provider,minus,z,0.)
        cols.append((wp-wm)/(2*h));asym.append(float(np.linalg.norm((wp-w0)/h-(w0-wm)/h)))
    return np.column_stack(cols),max(asym)

def desired_rate(ref,p,q,w,cfg):
    i,s=p.i,p.s; r,vr,tau=ref.geometry(i,s); n=np.array([-tau[1],tau[0]])
    dq=q[:2]-r[:2];cross=float(n@dq);along=float(tau@dq)
    v=velocity(q,w); L=cfg['lookahead_m']; h=cfg['gradient_difference_m']
    gr,ar=gradient(ref.provider,r,ref.z,h)
    # Reuse derivative only when it is literally the same point (nominal invariance).
    g,aq=(gr,ar) if np.array_equal(q[:2],r[:2]) else gradient(ref.provider,q,ref.z,h)
    aref=np.array([math.cos(r[2]),math.sin(r[2])]); uref=float(ref.tr['controls'][i])
    gv=gr@vr; speed=float(np.linalg.norm(vr))
    chi_prime=float((VA*(vr@aref)*uref+vr[0]*gv[1]-vr[1]*gv[0])/(vr@vr))
    denom=speed-chi_prime*cross
    status='INTERIOR_FRENET'; fallback=None
    if p.complete:
        sdot=0.;status='COMPLETED_FIXED_TANGENT'
    elif denom<=cfg['projection_singularity_relative']*speed:
        sdot=0.;status='F0_FALLBACK_SINGULAR_OR_NONMINIMUM_PROJECTION';fallback=uref
    else:
        sdot=float(tau@v)/denom
        if s<=float(ref.starts[i])+1e-10 and sdot<0:
            sdot=0.;status='START_ACTIVE_CONSTRAINT_ONE_SIDED'
        if s>=float(ref.ends[i])-1e-10 and sdot>0:
            sdot=0.;status='END_ACTIVE_CONSTRAINT_ONE_SIDED'
    edot=float(n@v)-chi_prime*sdot*along
    chi=math.atan2(tau[1],tau[0])-math.atan2(cross,L)
    chidot=chi_prime*sdot-L/(L*L+cross*cross)*edot
    td=np.array([math.cos(chi),math.sin(chi)]);nd=np.array([-td[1],td[0]])
    b=float(nd@w)/VA; discr=1.-b*b
    if discr<=0:raise StructuralStop('DESIRED_RATE_INVERSE_DISCRIMINANT')
    root=math.sqrt(discr)
    ff=chidot*(1.+float(td@w)/(VA*root))-float(nd@(g@v))/(VA*root)
    if fallback is not None:ff=fallback
    if not math.isfinite(ff):raise StructuralStop('NONFINITE_DESIRED_RATE')
    return float(ff),dict(derivative_status=status,reference_feedforward=0. if p.complete else uref,
        desired_rate_rad_s=float(ff),desired_rate_outside_bound=abs(ff)>VA/R0,
        projection_along_residual_m=along,cross_track_start_m=cross,s_dot=sdot,
        course_rate_rad_s=chidot,projection_denominator_m_s=denom,
        gradient_one_sided_asymmetry=max(ar,aq),gradient_step_m=h,
        derivative_assurance='LOCAL_ORTHOGONAL_PROJECTION_AND_SAME_CELL_DIFFERENTIABILITY_REQUIRED',
        feedforward_fallback=fallback is not None)
