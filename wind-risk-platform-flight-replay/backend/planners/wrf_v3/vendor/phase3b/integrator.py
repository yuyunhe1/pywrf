"""Solver-only numerical RK45 propagation. Independent C does not import this."""
import math
import numpy as np
from scipy.integrate import solve_ivp
from phase3a_implementation.domain import VA, R0


class SpatialIntegrationError(RuntimeError): pass


def integrate(start, controls, durations, provider, z):
    state = np.asarray(start,float).copy()
    nodes = [state.tolist()]
    t0 = 0.
    for u,d in zip(controls,durations):
        if d>0:
            def rhs(t,q):
                s=provider.wind_query(float(q[0]),float(q[1]),z,max(0.,float(t)))
                if not s.get('valid',False): raise SpatialIntegrationError(s.get('status','WIND_QUERY_FAILED'))
                wx,wy=float(s['wx']),float(s['wy'])
                if not math.isfinite(wx) or not math.isfinite(wy): raise SpatialIntegrationError('NONFINITE_WIND')
                if math.hypot(wx,wy)/VA>=1.: raise SpatialIntegrationError('CONNECTOR_OUT_OF_DOMAIN')
                return [VA*math.cos(q[2])+wx,VA*math.sin(q[2])+wy,float(u)]
            sol=solve_ivp(rhs,(t0,t0+float(d)),state,method='RK45',rtol=2e-10,
                          atol=[R0*1e-11,R0*1e-11,1e-11],max_step=.4)
            if not sol.success or not np.isfinite(sol.y[:,-1]).all():
                raise SpatialIntegrationError('INTEGRATION_FAILURE')
            state=sol.y[:,-1]
        t0+=float(d)
        nodes.append(state.tolist())
    return state,nodes
