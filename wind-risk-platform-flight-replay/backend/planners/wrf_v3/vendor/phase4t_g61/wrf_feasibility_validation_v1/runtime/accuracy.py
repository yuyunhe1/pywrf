"""Shooting-only exact ZOH heading and scaled XY; independent physical C unchanged."""
import contextlib,math
import numpy as np
from scipy.integrate import solve_ivp
from unittest.mock import patch
from phase3a_implementation.domain import VA,R0
from phase3b.integrator import SpatialIntegrationError
POLICY=dict(version='ANALYTIC_ZOH_HEADING_DISPLACEMENT_XY_DOP853_V1',method='DOP853',
    rtol=1e-12,atol_scaled=[1e-13,1e-13],max_step_s=.4,
    max_step_rationale='same .4s cap as historical shooting; bounded heading advance <= .470rad; adaptive error control still active; no grid scan',
    state='[(x-x0)/R0,(y-y0)/R0]',heading='psi0+fsum(previous u*d)+u*(t-segment_start)',
    query='original global x=x0+R0*dx,y=y0+R0*dy,z,t',heading_lift='UNWRAPPED_NO_MODULO',
    reset_at_stored_nodes=False,independent_C_unchanged=True,
    numerical_assurance='FIXED_POLICY_TESTED_AGAINST_ORIGINAL_C_NOT_GLOBAL_ODE_CERTIFICATE')
def integrate(start,controls,durations,provider,z):
    origin=np.asarray(start,float).copy();uvec=np.asarray(controls,float);ds=np.asarray(durations,float)
    if origin.shape!=(3,) or ds.shape!=uvec.shape or ds.ndim!=1 or not np.isfinite(np.r_[origin,uvec,ds,z]).all() or np.any(ds<0):raise SpatialIntegrationError('INVALID_INPUT')
    state=np.zeros(2);nodes=[origin.tolist()];t0=0.;angles=[]
    for u,d in zip(uvec,ds):
        u=float(u);d=float(d);psi0=origin[2]+math.fsum(angles)
        if d>0:
            def rhs(t,s):
                x=origin[0]+R0*s[0];y=origin[1]+R0*s[1];psi=psi0+u*(float(t)-t0)
                w=provider.wind_query(float(x),float(y),float(z),max(0.,float(t)))
                if not w.get('valid'):raise SpatialIntegrationError(w.get('status','WIND_QUERY_FAILED'))
                wx,wy=float(w['wx']),float(w['wy'])
                if not math.isfinite(wx) or not math.isfinite(wy):raise SpatialIntegrationError('NONFINITE_WIND')
                if math.hypot(wx,wy)/VA>=1.:raise SpatialIntegrationError('CONNECTOR_OUT_OF_DOMAIN')
                return [(VA*math.cos(psi)+wx)/R0,(VA*math.sin(psi)+wy)/R0]
            sol=solve_ivp(rhs,(t0,t0+d),state,method='DOP853',rtol=POLICY['rtol'],atol=POLICY['atol_scaled'],max_step=POLICY['max_step_s'])
            if not sol.success or not np.isfinite(sol.y).all():raise SpatialIntegrationError('INTEGRATION_FAILURE')
            state=sol.y[:,-1]
        angles.append(u*d);t0+=d
        nodes.append([origin[0]+R0*state[0],origin[1]+R0*state[1],origin[2]+math.fsum(angles)])
    return np.asarray(nodes[-1]),nodes
@contextlib.contextmanager
def install():
    import phase3b.connector as spatial
    import phase4e.acceleration.integration as memo
    with patch.object(spatial,'integrate',integrate),patch.object(memo,'original_integrate',integrate):yield
