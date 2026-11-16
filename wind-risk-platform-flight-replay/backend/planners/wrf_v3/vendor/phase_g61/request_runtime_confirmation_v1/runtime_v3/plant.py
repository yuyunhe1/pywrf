"""Plant-private registered disturbance scheduler, with complete duration ledger.

Only observed requested/applied controls and actual states leave this object.
The solver receives no disturbance timestamps or future plant state.
"""
import math

U=10/8.513422179807296

class Plant:
    def __init__(self,doc):
        if doc.get('version')!='PRIVATE_FROZEN_ACTUATOR_PULSES_V1':raise ValueError('PRIVATE_PLANT_SCHEMA')
        self._faults=[];self._active=None;self.rows=[]
        for index,row in enumerate(doc['faults']):
            start,d=row['start_physical_s'],row['duration_s']
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in [start,d]) or start<0 or d<=0:raise ValueError('PRIVATE_FAULT_INTERVAL')
            if self._faults and start<self._faults[-1]['end']:raise ValueError('PRIVATE_FAULT_OVERLAP')
            self._faults.append(dict(index=index,start=float(start),duration=float(d),end=float(start)+float(d),used=0.,frozen_nominal=None,actual=None,complete=False))
        self._doc=doc

    def control(self,t,requested,maximum_duration):
        if not math.isfinite(t+requested+maximum_duration) or maximum_duration<=0 or abs(requested)>U+1e-12:raise ValueError('PLANT_COMMAND_SCHEMA')
        if self._active is not None and t>=self._active['end']-1e-12:
            active=self._active
            if abs(active['used']-active['duration'])>1e-10:raise ValueError('PRIVATE_FAULT_DURATION_LOST')
            active['complete']=True;self._active=None
        if self._active is None:
            for f in self._faults:
                if not f['complete'] and f['frozen_nominal'] is None and f['start']<=t+1e-12:
                    if t>f['start']+1e-10:raise ValueError('PRIVATE_FAULT_START_SKIPPED')
                    f['frozen_nominal']=float(requested);f['actual']=max(-U,min(U,float(requested)-.2*U));self._active=f;break
        active=self._active
        if active is not None:
            d=min(maximum_duration,active['end']-t)
            return float(active['actual']),float(d),dict(private_fault_index=active['index'],frozen_nominal=active['frozen_nominal'])
        future=min((f['start'] for f in self._faults if not f['complete'] and f['frozen_nominal'] is None and f['start']>t),default=math.inf)
        return float(requested),float(min(maximum_duration,future-t)),None

    def observe(self,start,used,requested,applied,marker):
        if used<=0 or not math.isfinite(used):raise ValueError('PLANT_OBSERVATION_DURATION')
        if marker is not None:
            active=self._active
            if active is None or active['index']!=marker['private_fault_index'] or applied!=active['actual']:raise ValueError('PLANT_ACTIVE_IDENTITY')
            active['used']=math.fsum((active['used'],float(used)))
            if active['used']>active['duration']+1e-10:raise ValueError('PRIVATE_FAULT_OVER_APPLIED')
        row=dict(start_physical_s=float(start),duration_s=float(used),requested_control=float(requested),actual_control=float(applied),
                 actuator_mismatch=bool(float(requested)!=float(applied)))
        self.rows.append(row)
        return row

    def assessment(self,t):
        # Mark an exactly completed final row even if no later command arrives.
        if self._active is not None and t>=self._active['end']-1e-12:
            if abs(self._active['used']-self._active['duration'])>1e-10:raise ValueError('PRIVATE_FAULT_DURATION_LOST')
            self._active['complete']=True;self._active=None
        return dict(faults=[dict(f) for f in self._faults],rows=self.rows,
            all_registered_pulses_fully_applied=all(f['complete'] for f in self._faults),
            requested_total_fault_duration_s=math.fsum(f['duration'] for f in self._faults),
            actually_applied_fault_duration_s=math.fsum(f['used'] for f in self._faults))
