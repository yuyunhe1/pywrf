"""Deterministic initialization after removal of an unwanted full heading turn.

The feasible control set, exact heading equation, XY root, seed order and all
acceptance gates are identical to HeadingModel. Only the starting point moves.
"""
import math
import numpy as np
from .heading_model import HeadingModel as BaseModel,FamilyReject
from phase3b.validator import THRESHOLDS

class RewoundHeadingModel(BaseModel):
    def __init__(self,controls,durations,start_heading,goal_heading,horizon,*,straight_hint=None):
        super().__init__(controls,durations,start_heading,goal_heading,horizon)
        self.initialization_certificate=dict(rule='ORIGINAL_LIFT_CONSTRAINED_START')
        if self.kind!='CSC_SAME_SIGN':return
        original_delta=math.fsum(float(u)*float(d) for u,d in zip(controls,durations))
        difference=original_delta-self.delta;turns=round(difference/(2*math.pi))
        if turns==0 or abs(difference-turns*2*math.pi)>THRESHOLDS['heading_rad']:return
        remove=difference/float(self.u[0])
        if remove<=0 or self.h<=0:return
        first=min(self.h,max(0.,float(durations[0])-remove))
        self.x0[0]=np.clip(first/self.h,1e-8,1.-1e-8)
        if straight_hint is not None and math.isfinite(straight_hint) and straight_hint>=0 and self.free>0:
            self.x0[1]=np.clip(straight_hint/self.free,1e-8,1.-1e-8)
        self.initialization_certificate=dict(rule='REMOVE_UNWANTED_FULL_TURN_FROM_FIRST_ARC_THEN_LAST',
            original_heading_delta=original_delta,required_heading_delta=self.delta,unwanted_turns=turns,
            removed_turn_time_s=remove,first_turn_after_reduction_s=first,straight_hint_s=straight_hint,
            required_lift=goal_heading,new_feasible_initial=self.x0.tolist(),old_seed_not_deleted=True)
