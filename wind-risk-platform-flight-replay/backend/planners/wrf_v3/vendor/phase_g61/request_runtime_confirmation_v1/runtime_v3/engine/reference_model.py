"""Single shooting from actual state with an immutable reference control seed.

No reference node is a reset. Two duration freedoms and one exact heading
elimination retain all unexecuted control segments and the original bounds.
"""
import math
from fractions import Fraction
import numpy as np
from phase_g61.online_execution_repair_v1.heading_model import OMEGA,FamilyReject
from phase_g61.online_execution_repair_v1.two_segment_model import TwoSegmentModel

def suffix_seed(route,offset):
    cut=Fraction(float(offset));t=Fraction(0);controls=[];durations=[]
    for u,d in zip(route['controls'],route['durations']):
        end=t+Fraction(float(d))
        if end>cut:
            controls.append(float(u));durations.append(float(end-max(t,cut)))
        t=end
    if not controls:raise FamilyReject('NO_UNEXECUTED_REFERENCE_SUFFIX')
    return controls,durations

class ReferenceModel:
    def __init__(self,route,offset,start_heading,goal_heading,horizon):
        self.u,self.ds=map(np.asarray,suffix_seed(route,offset));self.H=float(horizon);self.delta=float(goal_heading-start_heading);self.two=None
        if not math.isfinite(self.H) or self.H<=0 or not np.isfinite(self.u).all() or np.max(np.abs(self.u))>OMEGA+1e-12:raise FamilyReject('REFERENCE_MODEL_DOMAIN')
        if len(self.u)==2 and self.u[0]==0.:
            self.two=TwoSegmentModel(start_heading,goal_heading,self.H,float(self.ds[0]));self.kind='REFERENCE_SEEDED_TWO_BOUNDED_ZOH';self.n=2
            self.lower=self.two.lower;self.upper=self.two.upper;self.x0=self.two.x0;return
        zero=[i for i,u in enumerate(self.u) if u==0.];turn=[i for i,u in enumerate(self.u) if u!=0.]
        if not zero or len(turn)<1:raise FamilyReject('REFERENCE_NEEDS_STRAIGHT_AND_COMPENSATING_TURN')
        self.k=turn[-1];angles=[float(start_heading)+math.fsum(float(u*d) for u,d in zip(self.u[:i],self.ds[:i])) for i in range(len(self.u))]
        choices=[(float(min(self.ds[i],self.ds[j]))*abs(math.sin(angles[i]-angles[j])),-i,-j,i,j) for i in zero for j in zero if i<j and abs(math.sin(angles[i]-angles[j]))>1e-5]
        best=max(choices,default=None)
        if best:self.i,self.j=best[-2:];self.selection='MAX_MIN_STRAIGHT_DURATION_TIMES_DIRECTION_SINE_FIXED_TIE_ORDER'
        else:
            other=[i for i in turn if i!=self.k]
            if not other:raise FamilyReject('REFERENCE_DURATION_FREEDOMS_RANK_INSUFFICIENT')
            self.i=max(other,key=lambda i:(self.ds[i],-i));self.j=max(zero,key=lambda i:(self.ds[i],-i));self.selection='LONGEST_NONCOMPENSATING_TURN_AND_STRAIGHT'
        fixed=[i for i in range(len(self.u)) if i not in (self.i,self.j,self.k)]
        self.fixed_time=math.fsum(float(self.ds[i]) for i in fixed)
        self.a=(self.delta-math.fsum(float(self.u[i]*self.ds[i]) for i in fixed))/self.u[self.k]
        self.alpha=self.u[self.i]/self.u[self.k]
        # d_k=a-alpha*d_i; d_j is straight. Intersect d_k>=0 and
        # sum(d)<=H exactly as linear half spaces before box parameterization.
        lo=0.;hi=self.H
        for b,c in [(self.a,self.alpha),(self.H-self.fixed_time-self.a,1.-self.alpha)]:
            if c>0:hi=min(hi,b/c)
            elif c<0:lo=max(lo,b/c)
            elif b<0:raise FamilyReject('REFERENCE_NO_NONNEGATIVE_DURATION_POLYTOPE')
        if not math.isfinite(lo+hi) or hi<=lo:raise FamilyReject('REFERENCE_EMPTY_DURATION_INTERVAL')
        self.lo=max(0.,lo);self.hi=hi;di=min(hi,max(self.lo,float(self.ds[self.i])));djmax=self.H-self.fixed_time-self.a-(1.-self.alpha)*di
        if djmax<=0:raise FamilyReject('REFERENCE_ZERO_SECOND_DURATION_INTERVAL')
        self.x0=np.array([(di-self.lo)/(self.hi-self.lo),float(self.ds[self.j])/djmax]);self.x0=np.clip(self.x0,1e-8,1-1e-8)
        self.lower=np.zeros(2);self.upper=np.ones(2);self.n=2;self.kind='REFERENCE_SUFFIX_N_SEGMENT_EXACT_LIFT_SINGLE_SHOOTING'
    def decode(self,x):
        if self.two is not None:return self.two.decode(x)
        x=np.asarray(x,float)
        if x.shape!=(2,) or not np.isfinite(x).all() or np.any(x<0) or np.any(x>1):raise ValueError('REFERENCE_MODEL_VARIABLE_DOMAIN')
        ds=self.ds.copy();di=self.lo+(self.hi-self.lo)*x[0];ds[self.i]=di
        ds[self.k]=self.a-self.alpha*di
        djmax=self.H-self.fixed_time-self.a-(1.-self.alpha)*di;ds[self.j]=max(0.,djmax)*x[1]
        if np.min(ds)<-1e-11:raise ValueError('REFERENCE_NEGATIVE_DECODE')
        return self.u.copy(),ds
