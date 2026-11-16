"""One continuous actual RK45 solve per held ZOH command.

Every original 20 ms actual checkpoint and its fsync remain. Only repeated ODE
solver construction inside an unchanged control hold disappears. No solution
is shared with C, collision, reference, or another held command.
"""
import math
import numpy as np
from scipy.integrate import solve_ivp
from phase3a_implementation.domain import R0,VA
from phase4t.amendment_v1.model import wind,velocity,StructuralStop

POLICY=dict(version='ACTUAL_HELD_ZOH_CONTINUOUS_RK45_DENSE_CHECKPOINT_V1',method='RK45',rtol=2e-12,
    atol=[R0*1e-13,R0*1e-13,1e-13],max_step_s=.05,checkpoint_s=.02,
    checkpoint_fsync='EVERY_ORIGINAL_APPLIED_20MS_ROW',independent_validators_share_solution=False,
    applicable_role='FIXED_KNOWN_ZOH_CONTROL_ONLY_NOT_FEEDBACK_CONTROL_CHANGES')

def propagate(q,u,d,provider,z,t):
    if not math.isfinite(u) or abs(u)>VA/R0+1e-12 or not math.isfinite(d) or d<=0:raise StructuralStop('ACTUAL_HELD_CONTROL_DOMAIN')
    def rhs(time,state):
        v=velocity(state,wind(provider,state,z,time));return [v[0],v[1],u]
    sol=solve_ivp(rhs,(t,t+d),np.asarray(q,float),method='RK45',rtol=POLICY['rtol'],atol=POLICY['atol'],max_step=.05,dense_output=True)
    if not sol.success or not np.isfinite(sol.y).all():raise StructuralStop('ACTUAL_HELD_INTEGRATION_FAILURE')
    return sol
