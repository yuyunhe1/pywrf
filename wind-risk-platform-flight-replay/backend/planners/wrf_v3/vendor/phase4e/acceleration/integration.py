"""Optional exact-input RK45 memoization; independent C is always recomputed."""
from copy import deepcopy
import numpy as np
from .wind import LRU,bits
from phase3b.integrator import integrate as original_integrate
from phase4a_design.mock_contract import content_hash
from scipy.integrate import solve_ivp as original_solve_ivp

class ExactIntegrationMemo:
    def __init__(self,capacity=256):self.cache=LRU(capacity);self._providers={}
    def __call__(self,start,controls,durations,provider,z):
        # Provider object identity fixes source; exact float64 byte keys preserve
        # signed zero. The same frozen function/tolerances execute on a miss.
        arrays=[np.asarray(v,dtype=np.float64) for v in (start,controls,durations)]
        self._providers[id(provider)]=provider  # Prevent id reuse while entries exist.
        source=content_hash(provider.source)
        wind=tuple(provider.wind) if getattr(provider,'kind',None)=='SYNTHETIC_CONSTANT' else None
        key=(id(provider),source,wind,bits(float(z)),tuple((a.shape,a.tobytes()) for a in arrays))
        value=self.cache.get(key)
        if value is not None:return value[0].copy(),deepcopy(value[1])
        endpoint,nodes=original_integrate(start,controls,durations,provider,z)
        self.cache.put(key,(endpoint.copy(),deepcopy(nodes)))
        return endpoint,nodes
    def stats(self):return self.cache.summary()

class ExactRK45SegmentMemo:
    """Cache only the original solver's identical RK45 segment calls.

    Absolute t_span is part of the key: no time-origin reset or alternative ODE
    is introduced. C imports its own solve_ivp and never uses this memo.
    """
    def __init__(self,capacity=2048):self.cache=LRU(capacity);self.providers={};self.fallback_calls=0
    def __call__(self,fun,t_span,y0,**kwargs):
        cells={name:cell.cell_contents for name,cell in zip(fun.__code__.co_freevars,fun.__closure__ or ())}
        if (fun.__module__!='phase3b.integrator' or fun.__code__.co_name!='rhs' or
            set(cells)!={'provider','u','z'} or kwargs.get('method')!='RK45'):
            self.fallback_calls+=1;return original_solve_ivp(fun,t_span,y0,**kwargs)
        provider=cells['provider'];self.providers[id(provider)]=provider
        state=np.asarray(y0,dtype=np.float64)
        key=(fun.__code__,id(provider),content_hash(provider.source),bits(float(cells['u']),float(cells['z']),
            float(fun.__globals__['VA']),float(fun.__globals__['R0']),*map(float,t_span)),
            state.shape,state.tobytes(),content_hash(kwargs))
        value=self.cache.get(key)
        if value is not None:return deepcopy(value)
        sol=original_solve_ivp(fun,t_span,y0,**kwargs)
        if sol.success:self.cache.put(key,deepcopy(sol))
        return sol
    def stats(self):return dict(**self.cache.summary(),fallback_calls=self.fallback_calls)
