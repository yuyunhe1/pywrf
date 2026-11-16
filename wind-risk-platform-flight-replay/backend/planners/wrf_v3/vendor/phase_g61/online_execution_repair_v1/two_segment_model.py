"""Two bounded ZOH controls with exact terminal air-heading lift."""
import math
import numpy as np
from .heading_model import OMEGA,FamilyReject

class TwoSegmentModel:
    def __init__(self,start_heading,goal_heading,horizon,first_duration_hint):
        self.delta=float(goal_heading-start_heading);self.H=float(horizon);self.sign=1. if self.delta>=0 else -1.;self.D=abs(self.delta)
        if not math.isfinite(self.H) or not math.isfinite(self.delta) or self.H<=0 or self.D>OMEGA*self.H:raise FamilyReject('HEADING_MINIMUM_TIME_EXCEEDS_HORIZON')
        self.kind='TWO_SEGMENT_BOUNDED_CONTROL';self.n=2;self.lower=np.zeros(2);self.upper=np.ones(2)
        first=min(self.H,max(0.,float(first_duration_hint)));first=min(self.H*(1-1e-8),max(self.H*1e-8,first))
        lo=max(0.,self.D-OMEGA*first);hi=min(self.D+OMEGA*first,OMEGA*(self.H-first));a=min(hi,max(lo,self.D))
        self.x0=np.array([first/self.H,(a-lo)/(hi-lo) if hi>lo else .5]);self.x0=np.clip(self.x0,1e-8,1-1e-8)
    def decode(self,x):
        x=np.asarray(x,float)
        if x.shape!=(2,) or not np.isfinite(x).all():raise ValueError('MODEL_VARIABLES')
        t=self.H*x[0];lo=max(0.,self.D-OMEGA*t);hi=min(self.D+OMEGA*t,OMEGA*(self.H-t));a=lo+x[1]*max(0.,hi-lo)
        first_angle=self.delta-self.sign*a;u=first_angle/t if t>0 else 0.
        return np.array([u,self.sign*OMEGA]),np.array([t,a/OMEGA])
