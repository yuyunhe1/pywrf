"""Enclose vertical contacts after committed-prefix reintegration.

The provisional dense root is not assumed to be the reintegrated root. A local
strict-monotonicity enclosure and native rounding bound may widen its bracket
within the original 1e-7 s cap. LOWER/UPPER replays integrate to their own widened
endpoints. No wind arithmetic, states, controls or acceptance thresholds change.
"""
import math
import numpy as np
from numpy.polynomial import Polynomial as P
from phase3a_implementation.domain import R0
from phase4t_g61.wrf_feasibility_validation_v1.runtime import boundaries as original

POLICY=dict(version='COMMITTED_PREFIX_NATIVE_VERTICAL_CONTACT_ENCLOSURE_V1',
    applies='STRICTLY_MONOTONE_VERTICAL_ROOT_INSIDE_ONE_NATIVE_HORIZONTAL_CELL',
    maximum_bracket_width_s=original.POLICY['max_bracket_width_s'],
    state='REINTEGRATE_FROM_OWN_CURRENT_STATE_TO_CHOSEN_BOUND_NO_SNAP',
    sensitivity='ORIGINAL_INDEPENDENT_LOWER_UPPER_RUNS_USE_ENCLOSED_CONTACT_ENDPOINTS',
    assurance='CONSERVATIVE_FLOAT_ROUNDING_LOCALIZATION_NOT_INTERVAL_ODE_CERTIFICATE',
    unsupported='GRAZING_OR_CELL_CROSSING_OR_WIDE_ENCLOSURE_RETAINS_ORIGINAL_CONSERVATIVE_BEHAVIOR')

def power_range(poly,pad=0.):
    c=np.asarray(poly.coef,float)
    return float(c[0]+sum(min(0.,v) for v in c[1:])-pad),float(c[0]+sum(max(0.,v) for v in c[1:])+pad)

def enclosure(engine,event,dense,committed_state):
    axis=event['axis']
    if axis=='height':raw=engine.raw
    elif axis.startswith('height_source_'):
        i=int(axis.removeprefix('height_source_'))
        if not 0<=i<len(engine._detectors):raise original.BoundaryUnresolved('VERTICAL_SOURCE_INDEX')
        raw=engine._detectors[i].raw
    else:return None
    k=event['index'];coord=raw.coordinates;polys=original.polynomials(dense);ii,jj=coord.local_to_grid(*map(float,engine.origin[:2]))
    ip=P([ii])+polys[0]*(R0*coord.m0/coord.dx);jp=P([jj])+polys[1]*(R0*coord.m0/coord.dy)
    eps=np.finfo(float).eps;grid_pad=256*eps*max(1.,coord.nx,coord.ny,abs(ii),abs(jj));ir=power_range(ip,grid_pad);jr=power_range(jp,grid_pad)
    i0,j0=math.floor(ip(.5)),math.floor(jp(.5))
    if not (0<=i0<coord.nx-1 and 0<=j0<coord.ny-1 and i0<ir[0]<=ir[1]<i0+1 and j0<jr[0]<=jr[1]<j0+1):return None
    field=np.asarray(raw.height[k,j0:j0+2,i0:i0+2],float)
    if not np.isfinite(field).all():return None
    a,b=ip-i0,jp-j0
    hp=P([float(field[0,0])-engine.z])+(field[0,1]-field[0,0])*a+(field[1,0]-field[0,0])*b+(field[1,1]-field[0,1]-field[1,0]+field[0,0])*a*b
    scale=max(1.,abs(engine.z),float(np.max(np.abs(field))));deriv=hp.deriv();derivative_pad=256*eps*(scale+float(np.sum(np.abs(deriv.coef))))
    dl,dh=power_range(deriv,derivative_pad)
    if dl>0:direction=1;slope=dl/abs(dense.h)
    elif dh<0:direction=-1;slope=-dh/abs(dense.h)
    else:return None
    if direction!=event['direction']:return None
    xy=engine.origin[:2]+R0*np.asarray(committed_state[:2]);interp,*_=raw._horizontal(float(xy[0]),float(xy[1]));residual=float(interp(raw.height)[k])-engine.z
    gi=max(abs(field[0,1]-field[0,0]),abs(field[1,1]-field[1,0]));gj=max(abs(field[1,0]-field[0,0]),abs(field[1,1]-field[0,1]))
    rounding=(gi+gj)*grid_pad+256*eps*scale
    radius=math.nextafter((abs(residual)+rounding)/slope,math.inf)
    lo=event['bracket_s'][0]-radius;hi=event['bracket_s'][1]+radius
    if lo<dense.t_old or hi>dense.t or hi-lo>POLICY['maximum_bracket_width_s']:return None
    return dict(bracket_s=[lo,hi],width_s=hi-lo,original_provisional_bracket_s=list(event['bracket_s']),
        committed_native_residual_m=residual,native_rounding_bound_m=rounding,strict_height_slope_lower_m_per_s=slope,
        proof='POWER_COEFFICIENT_RANGE_STRICT_MONOTONICITY_AND_ONE_NATIVE_CELL',policy=POLICY)

def integrate(engine,a,b,state,u):
    start=float(a);current=np.asarray(state,float).copy();pieces=[]
    for _ in range(original.POLICY['max_events_per_run']+1):
        trial=original.stream_trial(engine,start,b,current,u);event=trial.event
        if event is None:
            pieces.append(dict(a=start,b=float(b),sol=trial.sol,start_state=current.tolist(),end_state=trial.y[:,-1].tolist()));current=trial.y[:,-1];break
        if len(engine.events)>=original.POLICY['max_events_per_run']:raise original.BoundaryUnresolved('BOUNDARY_EVENT_LIMIT')
        t=event['t'];prefix=engine.solve(start,t,current,u,'committed_prefix');end_state=prefix.y[:,-1]
        expanded=enclosure(engine,event,trial.sol.interpolants[-1],end_state)
        if expanded is not None:
            lo,hi=expanded['bracket_s'];chosen={'LOWER':lo,'MID':(lo+hi)/2,'UPPER':hi}[engine.choice]
            if chosen<=start:raise original.BoundaryUnresolved('COMMITTED_ENCLOSURE_NONADVANCING',expanded)
            event['committed_prefix_enclosure']=expanded;event['provisional_t']=t;event['bracket_s']=[lo,hi];event['width_s']=hi-lo
            if chosen!=t:prefix=engine.solve(start,chosen,current,u,'committed_prefix');end_state=prefix.y[:,-1]
            t=chosen;event['t']=t
        event.update(state_before_restart=end_state.tolist(),state_after_restart=end_state.tolist(),context='OWN_COMMITTED_PREFIX_AND_SOURCE_ROUNDING_ENCLOSURE',u=float(u))
        engine.events.append(event);engine.last_event=event;engine.stats['discarded_trial_tail_steps']+=int(np.sum(trial.t>t))
        pieces.append(dict(a=start,b=t,sol=prefix.sol,start_state=current.tolist(),end_state=end_state.tolist()));current=end_state;start=t
    else:raise original.BoundaryUnresolved('NONTERMINATING_BOUNDARY_REPLAY')
    engine.pieces.extend(dict(a=p['a'],b=p['b'],start_state=p['start_state'],end_state=p['end_state']) for p in pieces)
    def dense(t):
        i=min(int(np.searchsorted([p['b'] for p in pieces],t,side='left')),len(pieces)-1);return pieces[i]['sol'](float(t))
    return current,dense
