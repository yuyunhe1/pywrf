"""Exact air-heading elimination for bounded ZOH controls; wind remains in XY.

These models parameterize the original control bounds. They do not make a
reachability or completeness claim for the finite ordered shooting pool.
"""
import math
import numpy as np
from phase3a_implementation.domain import VA,R0
OMEGA=VA/R0

class FamilyReject(ValueError):pass

class HeadingModel:
    def __init__(self,controls,durations,start_heading,goal_heading,horizon):
        self.u=np.asarray(controls,float);ds=np.asarray(durations,float);self.H=float(horizon);self.delta=float(goal_heading-start_heading)
        if len(self.u)!=len(ds) or not 1<=len(ds)<=3 or not np.isfinite(np.r_[self.u,ds,self.H,self.delta]).all() or np.any(ds<0) or self.H<=0:raise FamilyReject('MODEL_INPUT')
        if np.any(np.abs(self.u)>OMEGA+1e-12):raise FamilyReject('CONTROL_INPUT')
        self.turn_min=abs(self.delta)/OMEGA
        if self.turn_min>self.H:raise FamilyReject('HEADING_MINIMUM_TIME_EXCEEDS_REMAINING_HORIZON')
        tiny=1e-12
        if len(ds)==1:
            self.kind='VARIABLE_SINGLE_ZOH';self.n=1
            self.x0=np.array([(min(self.H,max(self.turn_min,float(ds[0])))-self.turn_min)/max(tiny,self.H-self.turn_min)])
        elif len(ds)==2:
            self.kind='VARIABLE_TWO_ZOH';self.n=3
            total=min(self.H,max(self.turn_min,float(math.fsum(ds))))
            rho=float(ds[0]/math.fsum(ds)) if math.fsum(ds)>0 else .5;rho=min(1.-1e-8,max(1e-8,rho))
            t1,t2=total*rho,total*(1-rho);lo=max(-OMEGA*t1,self.delta-OMEGA*t2);hi=min(OMEGA*t1,self.delta+OMEGA*t2)
            alpha=min(hi,max(lo,float(self.u[0]*ds[0])));b=(alpha-lo)/max(tiny,hi-lo)
            self.x0=np.array([(total-self.turn_min)/max(tiny,self.H-self.turn_min),rho,b])
        elif abs(self.u[0])==OMEGA and self.u[1]==0. and abs(self.u[2])==OMEGA:
            self.n=2;h=self.delta/self.u[0];self.h=h
            if self.u[0]==self.u[2]:
                if h<0:raise FamilyReject('SAME_SIGN_TURNS_CANNOT_REACH_REQUIRED_SIGNED_HEADING')
                if h>self.H:raise FamilyReject('SAME_SIGN_TURN_TIME_EXCEEDS_HORIZON')
                self.kind='CSC_SAME_SIGN';self.free=self.H-h
                ratio=float(ds[0]/(ds[0]+ds[2])) if ds[0]+ds[2]>0 else .5
                self.x0=np.array([ratio,float(ds[1])/max(tiny,self.free)])
            else:
                self.kind='CSC_OPPOSITE_SIGN';self.free=self.H-abs(h)
                aa=max(0.,float((ds[0]-max(h,0.)+ds[2]-max(-h,0.))/2));aa=min(self.free/2,aa)
                self.x0=np.array([2*aa/max(tiny,self.free),float(ds[1])/max(tiny,self.free-2*aa)])
        elif abs(self.u[0])==OMEGA and self.u[2]==self.u[0] and self.u[1]==-self.u[0]:
            self.kind='CCC';self.n=2;self.h=self.delta/self.u[0]
            self.lo=max(0.,self.h);self.hi=(self.H+self.h)/2
            if self.hi<self.lo:raise FamilyReject('CCC_HEADING_TIME_EXCEEDS_HORIZON')
            outer=float(ds[0]+ds[2]);ratio=float(ds[0]/outer) if outer>0 else .5
            self.x0=np.array([(outer-self.lo)/max(tiny,self.hi-self.lo),ratio])
        elif np.all(self.u==0.):
            if self.delta!=0.:raise FamilyReject('STRAIGHT_CONTROLS_CANNOT_CHANGE_HEADING')
            self.kind='FIXED_STRAIGHT';self.n=1;total=math.fsum(ds)
            self.fractions=ds/total if total>0 else np.full(len(ds),1/len(ds));self.x0=np.array([total/self.H])
        else:raise FamilyReject('FIXED_CONTROL_PATTERN_NOT_PARAMETERIZED')
        self.x0=np.clip(self.x0,1e-8,1.-1e-8)
        self.lower=np.zeros(self.n);self.upper=np.ones(self.n)
        if self.kind=='VARIABLE_TWO_ZOH':self.lower[1]=1e-8;self.upper[1]=1.-1e-8

    def decode(self,x):
        x=np.asarray(x,float)
        if x.shape!=(self.n,) or not np.isfinite(x).all():raise ValueError('MODEL_VARIABLES')
        if self.kind=='VARIABLE_SINGLE_ZOH':
            total=self.turn_min+x[0]*(self.H-self.turn_min);u=0. if total==0 else self.delta/total
            return np.array([u]),np.array([total])
        if self.kind=='VARIABLE_TWO_ZOH':
            total=self.turn_min+x[0]*(self.H-self.turn_min);t1=total*x[1];t2=total*(1-x[1]);lo=max(-OMEGA*t1,self.delta-OMEGA*t2);hi=min(OMEGA*t1,self.delta+OMEGA*t2)
            alpha=lo+x[2]*(hi-lo)
            return np.array([alpha/t1 if t1 else 0.,(self.delta-alpha)/t2 if t2 else 0.]),np.array([t1,t2])
        if self.kind=='CSC_SAME_SIGN':return self.u,np.array([self.h*x[0],self.free*x[1],self.h*(1-x[0])])
        if self.kind=='CSC_OPPOSITE_SIGN':
            a=self.free*x[0]/2;return self.u,np.array([max(self.h,0.)+a,(self.free-2*a)*x[1],max(-self.h,0.)+a])
        if self.kind=='CCC':
            outer=self.lo+x[0]*(self.hi-self.lo);return self.u,np.array([outer*x[1],outer-self.h,outer*(1-x[1])])
        if self.kind=='FIXED_STRAIGHT':return self.u,self.fractions*(self.H*x[0])
        raise ValueError('MODEL_KIND')

    def key(self):return (self.kind,tuple(float(x).hex() for x in self.u),tuple(float(x).hex() for x in self.x0),float(self.H).hex(),float(self.delta).hex())
