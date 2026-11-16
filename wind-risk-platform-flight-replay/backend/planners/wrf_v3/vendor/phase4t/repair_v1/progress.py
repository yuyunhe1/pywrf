"""Controller geometric branch tracking. Projection follows actual displacement, never a positive floor."""
import numpy as np
from .contract import CONFIG

class OrderedProgress:
    def __init__(self,reference,q):
        self.ref=reference;self.i=0;self.s=0.;self.last=np.array(q,float)
        self.events=[];self.complete=False
    def observe(self,q,t):
        q=np.array(q,float);r,v,_=self.ref.geometry(self.i,self.s)
        guess=self.s+float((q[:2]-self.last[:2])@v/(v@v))
        self.s=self.ref.project(self.i,q,guess,CONFIG['projection_window_parameter_s'])
        self.last=q.copy()
        return self.s
    def boundary(self,q,t):
        end=float(self.ref.ends[self.i]);r,_,d=self.ref.geometry(self.i,end)
        # Neither time nor a predicted s authorizes a switch. A local forward
        # endpoint plane crossing is required, on the already tracked branch.
        if self.s<end-CONFIG['progress_parameter_roundoff_s']:return False
        if float((np.array(q)[:2]-r[:2])@d)<-CONFIG['crossing_roundoff_m']:return False
        if abs(float(q[2])-float(r[2]))>np.pi:return False # preserves angular lift/full loops
        self.events.append(dict(segment=self.i,t=float(t),reference_parameter_s=end,state=list(q)))
        self.s=end
        if self.i==len(self.ref.ends)-1:self.complete=True
        else:self.i+=1
        return True
    def endpoint_event(self):
        if self.complete or self.ref.ends[self.i]-self.s>.25:return None
        r,_,d=self.ref.geometry(self.i,float(self.ref.ends[self.i]))
        def event(t,q):return float((q[:2]-r[:2])@d)
        event.terminal=True;event.direction=1
        return event
