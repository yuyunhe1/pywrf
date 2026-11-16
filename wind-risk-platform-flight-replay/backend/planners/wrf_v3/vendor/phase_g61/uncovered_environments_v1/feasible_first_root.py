"""Stop endpoint polishing only to submit a candidate to ORIGINAL physical C.

The trigger is 0.1 of the existing position tolerance in R0-scaled XY. It is
not admission, optimizer convergence, or an ODE-count claim. If C rejects, the
existing ordered model pool remains available under the same deadline.
"""
from types import SimpleNamespace
import numpy as np
SCALED_CANDIDATE_TRIGGER=1e-7
def solve(fun,x0,backend,**kwargs):
    calls=0
    class Trigger(Exception):
        def __init__(self,x,y):self.x=x.copy();self.y=y.copy()
    def wrapped(x):
        nonlocal calls
        y=np.asarray(fun(x),float);calls+=1
        if y.shape==(2,) and np.isfinite(y).all() and np.max(np.abs(y))<=SCALED_CANDIDATE_TRIGGER:raise Trigger(np.asarray(x,float),y)
        return y
    try:
        result=backend.least_squares(wrapped,x0,**kwargs);result.callback_calls=calls;result.nfev_semantics='SCIPY_REPORTED_NFEV';return result
    except Trigger as found:return SimpleNamespace(x=found.x,fun=found.y,success=False,nfev=None,callback_calls=calls,
        nfev_semantics='SCIPY_RESULT_UNAVAILABLE_AT_FEASIBILITY_TRIGGER;CALLBACK_CALLS_RECORDED_SEPARATELY',termination='CANDIDATE_NEEDS_ORIGINAL_FULL_C_COLLISION')
