"""Operation guards retain full binding checks at entry/exit, light RHS checks.

Immutable source arrays and cached stencils never change during a calculation.
The original public query still checks everything unless inside a guarded
operation. Any forced mutation must reject the operation before publication.
"""
import contextlib,math,bisect,collections
from numbers import Real
from types import SimpleNamespace
import numpy as np
from .velocity_provider import VelocityTemporal as BaseTemporal,VelocitySuffix as BaseSuffix
from phase3a_implementation.adapter import AdapterError

class GuardedTemporal(BaseTemporal):
    @classmethod
    def from_records(cls,*a,**kw):
        obj=super().from_records(*a,**kw);object.__setattr__(obj,'_evaluation_state',SimpleNamespace(depth=0))
        return obj
    @contextlib.contextmanager
    def evaluation(self):
        self.assert_bound();state=self._evaluation_state;state.depth+=1
        try:yield self
        finally:
            state.depth-=1
            self.assert_bound()
            if state.depth<0:raise AdapterError('EVALUATION_GUARD_DEPTH')

    def wind_bound(self,x,y,z,t):
        if self._evaluation_state.depth<=0:raise AdapterError('EVALUATION_GUARD_REQUIRED')
        if self._live_signature()!=self._signature or tuple(id(p) for p in self._velocity_packs)!=self._velocity_pack_identity:raise AdapterError('TEMPORAL_BINDING_CHANGED')
        self._velocity_stats['queries']+=1
        result=dict(valid=False,status=None,wx=None,wy=None,wz=None,frame_id=self.frame_id,height_datum=self.height_datum)
        try:
            if any(isinstance(v,(bool,np.bool_)) for v in (x,y,z,t)):raise AdapterError('INVALID_INPUT')
            x,y,z,t=map(float,(x,y,z,t))
            if not all(math.isfinite(v) for v in (x,y,z,t)) or t<0:raise AdapterError('INVALID_INPUT')
            q=self._epoch_offset+t
            if q<self._offsets[0] or q>self._offsets[-1]:raise AdapterError('OUT_OF_TIME_DOMAIN')
            j=bisect.bisect_left(self._offsets,q)
            if q==self._offsets[j]:pairs=[(j,1.)]
            else:
                i=j-1;gap=self._offsets[j]-self._offsets[i]
                if gap>self.max_gap_s:raise AdapterError('TIME_GAP_EXCEEDS_CONTRACT')
                weight=(q-self._offsets[i])/gap;pairs=[(i,1.-weight),(j,weight)]
            values=[(self.native(i,x,y,z),weight) for i,weight in pairs]
            result.update(wx=math.fsum(v[0]*w for v,w in values),wy=math.fsum(v[1]*w for v,w in values),wz=math.fsum(v[2]*w for v,w in values),valid=True,status='OK')
        except AdapterError as exc:result['status']=exc.status
        except (TypeError,ValueError,OverflowError):result['status']='INVALID_INPUT'
        self._velocity_stats[result['status']]+=1
        if result['valid'] and math.hypot(result['wx'],result['wy'])>=10.:self._velocity_stats['CONNECTOR_OUT_OF_DOMAIN']+=1
        return result

class GuardedSuffix(BaseSuffix):
    def wind_query(self,x,y,z,t):
        raw=self.provider
        if getattr(raw,'_evaluation_state',None) is None or raw._evaluation_state.depth<=0:return super().wind_query(x,y,z,t)
        if self._live_signature()!=self._signature:raise AdapterError('SUFFIX_BINDING_CHANGED')
        if isinstance(t,(bool,np.bool_)) or not isinstance(t,Real) or not math.isfinite(t) or t<0:raise AdapterError('INVALID_SUFFIX_TIME')
        return raw.wind_bound(x,y,z,self.physical_offset_s+float(t))
    __call__=wind_query

def evaluation(provider):
    current=provider
    # Timed is an immutable wrapper; verify its own guard in each real query.
    if type(current).__name__=='Timed':current=current.original
    if isinstance(current,GuardedSuffix):current=current.provider
    return current.evaluation() if isinstance(current,GuardedTemporal) else contextlib.nullcontext()
