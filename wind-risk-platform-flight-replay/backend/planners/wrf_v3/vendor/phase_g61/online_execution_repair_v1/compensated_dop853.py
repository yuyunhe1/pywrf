"""DOP853 with compensated accepted-step state addition, SciPy 1.15.3.

Only the accepted-state addition changes. The original tableau, embedded error
estimate, tolerances, step controller, independent dense output and boundary
detector remain in use. This is floating point numerical evidence, not an
interval error proof. Rejected trial compensation is never committed.
"""
import math
import numpy as np
import scipy
from scipy.integrate import DOP853, solve_ivp as scipy_solve_ivp
from scipy.integrate._ivp.rk import MIN_FACTOR, MAX_FACTOR, SAFETY

POLICY=dict(version='COMPENSATED_ACCEPTED_DOP853_V1',scipy='1.15.3',
    change='ERROR_FREE_TWO_SUM_ACCEPTED_STATE_ADDITION_WITH_CARRIED_RESIDUAL',
    unchanged='TABLEAU_ERROR_NORM_TOLERANCES_CONTROLLER_DENSE_BOUNDARIES_AND_ALL_ORDER_THRESHOLDS',
    assurance='FLOATING_POINT_ENGINEERING_NOT_INTERVAL_CERTIFICATE')

def two_sum(a,b):
    value=a+b;v=value-a
    return value,(a-(value-v))+(b-v)

class CompensatedDOP853(DOP853):
    def __init__(self,*args,**kwargs):
        if scipy.__version__!=POLICY['scipy']:raise ValueError('COMPENSATED_SCIPY_VERSION_MISMATCH')
        super().__init__(*args,**kwargs)
        self._addition_residual=np.zeros_like(self.y)

    def _step_impl(self):
        t=self.t;y=self.y;max_step=self.max_step;rtol=self.rtol;atol=self.atol
        min_step=10*np.abs(np.nextafter(t,self.direction*np.inf)-t)
        if self.h_abs>max_step:h_abs=max_step
        elif self.h_abs<min_step:h_abs=min_step
        else:h_abs=self.h_abs
        step_accepted=False;step_rejected=False
        while not step_accepted:
            if h_abs<min_step:return False,self.TOO_SMALL_STEP
            h=h_abs*self.direction;t_new=t+h
            if self.direction*(t_new-self.t_bound)>0:t_new=self.t_bound
            h=t_new-t;h_abs=np.abs(h);K=self.K
            K[0]=self.f
            for s,(a,c) in enumerate(zip(self.A[1:],self.C[1:]),start=1):
                dy=np.dot(K[:s].T,a[:s])*h
                K[s]=self.fun(t+c*h,y+dy)
            increment=h*np.dot(K[:-1].T,self.B)
            increment,residual1=two_sum(increment,self._addition_residual)
            y_new,residual2=two_sum(y,increment)
            pending_residual=residual1+residual2
            f_new=self.fun(t+h,y_new);K[-1]=f_new
            scale=atol+np.maximum(np.abs(y),np.abs(y_new))*rtol
            error_norm=self._estimate_error_norm(K,h,scale)
            if error_norm<1:
                factor=MAX_FACTOR if error_norm==0 else min(MAX_FACTOR,SAFETY*error_norm**self.error_exponent)
                if step_rejected:factor=min(1,factor)
                h_abs*=factor;step_accepted=True
            else:
                h_abs*=max(MIN_FACTOR,SAFETY*error_norm**self.error_exponent);step_rejected=True
        self.h_previous=h;self.y_old=y;self.t=t_new;self.y=y_new
        self.h_abs=h_abs;self.f=f_new;self._addition_residual=pending_residual
        return True,None

def solve_ivp(*args,**kwargs):
    if kwargs.get('method')=='DOP853':kwargs['method']=CompensatedDOP853
    return scipy_solve_ivp(*args,**kwargs)
