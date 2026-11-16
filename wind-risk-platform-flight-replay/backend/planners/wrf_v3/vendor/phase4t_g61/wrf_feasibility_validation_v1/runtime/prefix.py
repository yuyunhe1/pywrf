"""Frozen five-second policies; same actual initialization and physical horizon."""
import math
import numpy as np
from phase4t.amendment_v1.model import simulated_step,StructuralStop
from .trace import span

def schedule(m):return list(zip(m['prefix_policy']['controls'],m['prefix_policy']['durations']))
def predict(m,provider):
    q=np.array(m['actual_initial_state'],float);nodes=[q.tolist()];t=0.
    with span('predict_prefix_schedule'):
        for u,d in schedule(m):
            q=simulated_step(q,u,d,provider,m['z_layer_msl_m'],t);t+=d;nodes.append(q.tolist())
    return nodes
def apply(plant,end):
    """Continue the immutable scheduled prefix to t=5, then bounded abort hold."""
    m=plant.req['mission'];t=0.
    for u,d in schedule(m):
        t+=d;stop=min(end,t)
        if plant.t<stop-1e-13:plant.to_time(stop,u,'REGISTERED_PREFIX_'+m['prefix_policy']['name'])
        if end<=t+1e-13:return
    if plant.t<end-1e-13:plant.to_time(end,schedule(m)[-1][0],'REGISTERED_ABORT_LAST_PREFIX_CONTROL_HOLD')
def count(m):return len(m['prefix_policy']['controls'])
def matches(m,tr):
    n=count(m)
    return tr['controls'][:n]==m['prefix_policy']['controls'] and tr['durations'][:n]==m['prefix_policy']['durations']
