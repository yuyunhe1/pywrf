"""Existing exact-lift two-ZOH model, with the declared initial control bound.

Physical Va/R0 and independent C remain unchanged. This constructs a NEW first
planned route, never edits an old route, deletes its arcs or certifies recovery.
"""
import math
import numpy as np


class InitialTwoZOH:
    def __init__(self,start_heading,goal_heading,horizon,first_duration_hint,planning_omega):
        self.omega=float(planning_omega)
        if not math.isfinite(self.omega) or not 0<self.omega<=10/8.513422179807296:
            raise ValueError('INITIAL_PLANNED_RATE_BOUND')
        self.delta=float(goal_heading-start_heading);self.H=float(horizon)
        self.sign=1. if self.delta>=0 else -1.;self.D=abs(self.delta)
        if not math.isfinite(self.H) or not math.isfinite(self.delta) or self.H<=0 or self.D>self.omega*self.H:
            raise ValueError('INITIAL_HEADING_MINIMUM_TIME_EXCEEDS_HORIZON')
        first=min(self.H,max(0.,float(first_duration_hint)))
        first=min(self.H*(1-1e-8),max(self.H*1e-8,first))
        lo=max(0.,self.D-self.omega*first);hi=min(self.D+self.omega*first,self.omega*(self.H-first))
        a=min(hi,max(lo,self.D))
        self.x0=np.clip(np.array([first/self.H,(a-lo)/(hi-lo) if hi>lo else .5]),1e-8,1-1e-8)
        self.lower=np.zeros(2);self.upper=np.ones(2)

    def decode(self,x):
        x=np.asarray(x,float)
        if x.shape!=(2,) or not np.isfinite(x).all() or np.any(x<0) or np.any(x>1):
            raise ValueError('INITIAL_TWO_ZOH_VARIABLE_DOMAIN')
        t=self.H*x[0];lo=max(0.,self.D-self.omega*t)
        hi=min(self.D+self.omega*t,self.omega*(self.H-t))
        a=lo+x[1]*max(0.,hi-lo);first_angle=self.delta-self.sign*a
        u=first_angle/t if t>0 else 0.
        return np.array([u,self.sign*self.omega]),np.array([t,a/self.omega])


def attempt(engine,start,goal,backend,index,hint,horizon,fraction):
    """One deterministic finite-difference feasibility attempt, not a root scan."""
    from phase_g61.uncovered_environments_v1.feasible_first_root import solve
    from phase3a_implementation.domain import R0
    model=InitialTwoZOH(start[2],goal[2],horizon,hint,fraction*10/R0)
    row=dict(kind='INITIAL_TWO_BOUNDED_ZOH_EXISTING_MODEL_WITH_DECLARED_RESERVE',
             planning_omega=model.omega,H=model.H,hint_s=hint,initial=model.x0.tolist(),
             original_goal_raw_heading=float(goal[2]),status='STARTED',optimizer_calls=None)
    engine._initial_two_zoh_attempt=row
    calls=0
    def residual(x):
        nonlocal calls
        calls+=1;us,ds=model.decode(x)
        end,_=backend.integrate(np.asarray(start,float),us,ds,engine.provider,engine.z)
        return (end[:2]-np.asarray(goal,float)[:2])/R0
    try:
        result=solve(residual,model.x0,backend,bounds=(model.lower,model.upper),
                     ftol=1e-12,xtol=1e-12,gtol=1e-12,max_nfev=100)
        us,ds=model.decode(result.x)
        row.update(nfev=result.nfev,nfev_semantics=result.nfev_semantics,
                   residual_callback_calls=calls,optimizer_success=bool(result.success),
                   scaled_XY=result.fun.tolist(),controls=us.tolist(),durations=ds.tolist())
        if np.any(ds<=0):row['status']='NONPOSITIVE_DURATION_REJECT';return
        tr=engine._trajectory(np.asarray(start,float),np.asarray(goal,float),us,ds)
        c=engine._validation_for_candidate(tr,math.fsum(ds),index,'ENDPOINT_RECOVERY')
        row.update(C=c,status='C_PASS_PENDING_COLLISION_AND_ADMISSION' if c['valid'] else 'C_REJECT')
        if c['valid']:engine._candidate(tr,'LIVE_INITIAL_TWO_ZOH_DECLARED_RESERVE','ENDPOINT_RECOVERY',c,index)
    finally:
        row['residual_callback_calls']=calls
