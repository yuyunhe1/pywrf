"""Shooting only: integrate wind displacement, exact analytic ZOH air motion.

Each RHS still uses its current physical position and mission time. The original
independent C, collision, plant integrator and ordered replays are untouched.
"""
import math
import numpy as np
from scipy.integrate import solve_ivp
from phase3a_implementation.domain import VA,R0
from phase3b.integrator import SpatialIntegrationError

POLICY=dict(version='EXACT_AIR_DISPLACEMENT_WIND_REMAINDER_SHOOTING_V1',method='DOP853',
    rtol=1e-12,atol_scaled=[1e-13,1e-13],max_step_s=8.,
    state='WIND_DISPLACEMENT/R0_PER_COMPUTED_CONTINUOUS_SEGMENT',
    query='ORIGINAL_PROVIDER_AT_CURRENT_XY_EXACT_AIR_PLUS_WIND_DISPLACEMENT_AND_ABSOLUTE_MISSION_TIME',
    physical_node_reset=False,wind_value_cache=False,independent_C_changed=False,
    assurance='NUMERICAL_POLICY_VALIDATED_AGAINST_ANALYTIC_FIELDS_AND_ORIGINAL_INDEPENDENT_C')

def air(psi,u,t):
    h=.5*u*t;scale=VA*t*(math.sin(h)/h if h else 1.)
    return np.array([scale*math.cos(psi+h),scale*math.sin(psi+h)])

def integrate(start,controls,durations,provider,z):
    origin=np.asarray(start,float).copy();us=np.asarray(controls,float);ds=np.asarray(durations,float)
    if origin.shape!=(3,) or ds.shape!=us.shape or ds.ndim!=1 or not np.isfinite(np.r_[origin,us,ds,z]).all() or np.any(ds<0):raise SpatialIntegrationError('INVALID_INPUT')
    state=origin.copy();nodes=[state.tolist()];t0=0.;angles=[]
    for u,d in zip(us,ds):
        u,d=float(u),float(d);q0=state.copy();psi=origin[2]+math.fsum(angles)
        if d>0:
            def rhs(t,s):
                xy=q0[:2]+air(psi,u,float(t)-t0)+R0*s
                w=provider.wind_query(float(xy[0]),float(xy[1]),float(z),max(0.,float(t)))
                if not w.get('valid'):raise SpatialIntegrationError(w.get('status','WIND_QUERY_FAILED'))
                wx,wy=float(w['wx']),float(w['wy'])
                if not math.isfinite(wx) or not math.isfinite(wy):raise SpatialIntegrationError('NONFINITE_WIND')
                if math.hypot(wx,wy)>=VA:raise SpatialIntegrationError('CONNECTOR_OUT_OF_DOMAIN')
                return [wx/R0,wy/R0]
            sol=solve_ivp(rhs,(t0,t0+d),np.zeros(2),method='DOP853',rtol=POLICY['rtol'],atol=POLICY['atol_scaled'],max_step=POLICY['max_step_s'])
            if not sol.success or not np.isfinite(sol.y).all():raise SpatialIntegrationError('INTEGRATION_FAILURE')
            state[:2]=q0[:2]+air(psi,u,d)+R0*sol.y[:,-1]
        angles.append(u*d);state[2]=origin[2]+math.fsum(angles);t0+=d;nodes.append(state.tolist())
    return state,nodes
