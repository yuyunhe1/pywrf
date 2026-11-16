"""Original fixed physical handover and wall cutoff; no restart8s grant."""
import math
from fractions import Fraction
from dataclasses import dataclass

class WindowError(ValueError):pass
class WindowExhausted(WindowError):pass

def numbers(*values):
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in values):raise WindowError('CAUSAL_CLOCK_SCHEMA')

@dataclass(frozen=True)
class Window:
    physical_origin:float
    first_trigger_perf:float
    physical_handover:float
    first_deadline:float
    root_deadline:float
    physical_cap:float

    @classmethod
    def begin(cls,t,p,root_deadline,physical_cap):
        numbers(t,p,root_deadline,physical_cap)
        if t<0 or t+8.>physical_cap or p+8.>root_deadline:raise WindowExhausted('INITIAL8S_WINDOW_INSUFFICIENT')
        return cls(float(t),float(p),math.fsum((float(t),8.)),float(p)+8.,float(root_deadline),float(physical_cap))

    def at(self,observed_t,observed_perf):
        numbers(observed_t,observed_perf)
        if observed_t<self.physical_origin or observed_perf<self.first_trigger_perf:raise WindowError('OBSERVATION_BEFORE_TRIGGER')
        r=float(Fraction(8.)-(Fraction(float(observed_t))-Fraction(self.physical_origin)))
        deadline=min(self.first_deadline,float(observed_perf)+r,self.root_deadline)
        if r<=0 or observed_perf>=deadline:raise WindowExhausted('ORIGINAL_PREPARATION_WINDOW_EXHAUSTED')
        return dict(prefix_duration_s=r,prepare_deadline=deadline,physical_handover_s=self.physical_handover,
            first_prepare_deadline=self.first_deadline,owner_start=self.first_trigger_perf,
            observation_perf=float(observed_perf),physical_anchor_s=float(observed_t))
