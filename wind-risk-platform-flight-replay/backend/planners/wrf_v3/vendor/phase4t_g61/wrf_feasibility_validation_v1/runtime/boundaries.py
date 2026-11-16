"""Autonomous, request-local WRF partition events; never reads saved crossings.

Provisional DOP853 dense polynomials locate candidate events. A committed prefix
is reintegrated to the selected event bracket time, and its actual endpoint is
carried to the next piece. No reference node or grid coordinate resets a state.
"""
import math
import numpy as np
from numpy.polynomial import Polynomial as P
from scipy.integrate import solve_ivp, DOP853, OdeSolution
from types import SimpleNamespace
from phase3a_implementation.domain import VA,R0
from phase3a_implementation.adapter import StaticWRFWindAdapter,AdapterError
from phase4e.acceleration.wind import ExactCachedWRFWindProvider
from phase4t.amendment_v1.model import DomainFail

POLICY=dict(version='AUTONOMOUS_WRF_PARTITION_EVENTS_STREAM_V1',
    provisional_trial='PUBLIC_DOP853_STEP_STOP_AT_FIRST_OWN_EVENT',
    committed_prefix='UNCHANGED_SOLVE_IVP_REINTEGRATION_TO_OWN_BRACKET',
    speculative_tail='NOT_EVALUATED_AFTER_FIRST_EVENT_FULL_COMMITTED_CONTINUATION_STILL_REQUIRED',scipy_dense_schema='SCIPY_1_15_3_DOP853_F_Y_OLD_H',
    max_events_per_run=128,max_bracket_width_s=1e-7,root_iterations=80,
    root_imaginary_tolerance=1e-8,root_endpoint_tolerance=1e-12,
    initial_contact_ulps=64,grazing_derivative_relative=64*math.sqrt(np.finfo(float).eps),event_root_choices=['MID','LOWER','UPPER'],
    grazing='NUMERICAL_UNRESOLVED_IF_CONTACT_CANNOT_BE_ISOLATED',
    state_join='CONTINUOUS_ACTUAL_ODE_ENDPOINT_NO_SNAP',
    exclusion='DENSE_F_TRIANGLE_BOUND_XY_AND_BILINEAR_HEIGHT_SLOPE_BOX_WITH_64_EPS_PADDING',
    event_uncertainty='INDEPENDENT_LOWER_UPPER_REPLAY_SENSITIVITY_EMPIRICAL_NOT_RIGOROUS_BOUND')

class BoundaryUnresolved(ValueError):
    def __init__(self,reason,evidence=None):
        self.reason=reason;self.evidence=evidence or {};super().__init__(reason)

def polynomials(dense):
    if not all(hasattr(dense,n) for n in ['F','y_old','h','t_old']) or np.asarray(dense.F).shape!=(7,3):
        raise BoundaryUnresolved('UNSUPPORTED_DOP853_DENSE_SCHEMA')
    x=P([0.,1.]);out=[]
    for j in range(3):
        p=P([0.])
        for i,f in enumerate(reversed(dense.F)):
            p=(p+float(f[j]))*(x if i%2==0 else 1-x)
        out.append(p+float(dense.y_old[j]))
    return out

def roots(poly,lo=0.,hi=1.):
    coef=np.asarray(poly.coef)
    if not np.isfinite(coef).all():raise BoundaryUnresolved('NONFINITE_BOUNDARY_POLYNOMIAL')
    if not np.any(coef):return [] # A whole dense piece lying on a partition has no crossing.
    rr=poly.roots();out=[]
    for r in rr:
        if abs(float(np.imag(r)))<=POLICY['root_imaginary_tolerance']:
            x=float(np.real(r))
            if lo-POLICY['root_endpoint_tolerance']<=x<=hi+POLICY['root_endpoint_tolerance']:
                out.append(min(hi,max(lo,x)))
    return sorted(set(out))

def extent(poly):
    ts=[0.,1.,*roots(poly.deriv())];ys=[float(poly(t)) for t in ts]
    return min(ys),max(ys)

class PartitionReplay:
    def __init__(self,provider,origin,z,step,*,choice='MID'):
        if choice not in POLICY['event_root_choices']:raise BoundaryUnresolved('UNKNOWN_EVENT_ROOT_CHOICE')
        self.provider=provider;self.origin=np.asarray(origin,float);self.z=float(z);self.step=float(step);self.choice=choice
        raw=provider.reference if isinstance(provider,ExactCachedWRFWindProvider) else provider
        if getattr(raw,'kind',None)=='WRF_STATIC_SNAPSHOT' and not isinstance(raw,StaticWRFWindAdapter):raise BoundaryUnresolved('UNSUPPORTED_WRF_PROVIDER')
        self.raw=raw if isinstance(raw,StaticWRFWindAdapter) else None
        self.events=[];self.contacts=[];self.pieces=[];self.last_event=None
        self.stats=dict(actual_ODE_calls=0,trial_ODE_calls=0,committed_prefix_ODE_calls=0,RHS_evaluations=0,
            accepted_trial_steps=0,discarded_trial_tail_steps=0,root_function_calls=0,polynomial_root_calls=0,partition_box_probes=0,partition_box_exclusions=0)
    def rhs(self,u):
        def fun(t,s):
            x=self.origin[0]+R0*s[0];y=self.origin[1]+R0*s[1];psi=self.origin[2]+s[2]
            w=self.provider.wind_query(float(x),float(y),self.z,max(0.,float(t)))
            if not w.get('valid'):raise DomainFail(w.get('status','WIND_QUERY_FAILED'))
            wx,wy=float(w['wx']),float(w['wy'])
            if not math.isfinite(wx) or not math.isfinite(wy) or math.hypot(wx,wy)>=VA:raise DomainFail('CONNECTOR_OUT_OF_DOMAIN')
            return [(VA*math.cos(psi)+wx)/R0,(VA*math.sin(psi)+wy)/R0,float(u)]
        return fun
    def solve(self,a,b,state,u,kind):
        self.stats['actual_ODE_calls']+=1;self.stats[kind+'_ODE_calls']+=1
        sol=solve_ivp(self.rhs(u),(float(a),float(b)),state,method='DOP853',rtol=3e-14,atol=[1e-15]*3,max_step=self.step,dense_output=True)
        if not sol.success:raise BoundaryUnresolved('ORDERED_INTEGRATION_FAILURE')
        self.stats['RHS_evaluations']+=int(sol.nfev)
        if kind=='trial':self.stats['accepted_trial_steps']+=len(sol.t)-1
        return sol
    def value(self,dense,t,kind,index):
        self.stats['root_function_calls']+=1
        s=dense(float(t));x=self.origin[0]+R0*s[0];y=self.origin[1]+R0*s[1]
        if kind in ('i','j'):return float(self.raw.coordinates.local_to_grid(float(x),float(y))[0 if kind=='i' else 1])-index
        try:
            interp,*_=self.raw._horizontal(float(x),float(y));return float(interp(self.raw.height)[index])-self.z
        except AdapterError as exc:raise DomainFail(exc.status) from exc
    def candidates(self,dense):
        ps=polynomials(dense);coord=self.raw.coordinates;i,j=coord.local_to_grid(*map(float,self.origin[:2]))
        ip=P([i])+ps[0]*(R0*coord.m0/coord.dx);jp=P([j])+ps[1]*(R0*coord.m0/coord.dy)
        candidates=[];horizontal=[0.,1.]
        for kind,poly,n in [('i',ip,coord.nx),('j',jp,coord.ny)]:
            critical=roots(poly.deriv())
            monotone=not any(0.<c<1. for c in critical)
            raw_left=self.value(dense,float(dense.t_old),kind,0)
            raw_right=self.value(dense,float(dense.t),kind,0)
            low,high=extent(poly);low=min(low,raw_left,raw_right);high=max(high,raw_left,raw_right)
            padding=64*np.finfo(float).eps*max(1.,abs(low),abs(high))
            for index in range(max(0,math.ceil(low-padding)),min(n-1,math.floor(high+padding))+1):
                # A roundoff-level touch can have no real roots. Inspect dense
                # extrema as well; conservative unresolved, never step over it.
                if any(0.<c<1. and abs(float(poly(c))-index)<=padding for c in roots(poly.deriv())):
                    raise BoundaryUnresolved('GRAZING_HORIZONTAL_EXTREMUM',dict(axis=kind,index=int(index)))
                self.stats['polynomial_root_calls']+=1
                if monotone:
                    # Original rounded coordinate map is monotone under these
                    # positive affine maps. Equal-sided endpoint values cannot
                    # hide a crossing; never restart on an affine proxy ghost.
                    fl,fr=raw_left-index,raw_right-index
                    if fl==0. and fr==0.:continue
                    if fl*fr>0.:continue
                    if raw_right==raw_left:continue
                    selected=[min(1.,max(0.,(index-raw_left)/(raw_right-raw_left)))]
                else:selected=roots(poly-index)
                for x in selected:
                    if 0.<x<1. and abs(float(poly.deriv()(x)))<=POLICY['grazing_derivative_relative']*max(1e-30,float(np.max(np.abs(poly.coef[1:]),initial=0.))):
                        raise BoundaryUnresolved('GRAZING_HORIZONTAL_PARTITION',dict(axis=kind,index=int(index)))
                    candidates.append((x,kind,index));horizontal.append(x)
        # For vertical boundaries use every mass-height surface that can attain z
        # in each horizontal cell traversed by this provisional dense polynomial.
        cuts=sorted(set(horizontal))
        for left,right in zip(cuts[:-1],cuts[1:]):
            if right<=left:continue
            mid=(left+right)/2;i0=min(max(int(math.floor(ip(mid))),0),coord.nx-2);j0=min(max(int(math.floor(jp(mid))),0),coord.ny-2)
            h=self.raw.height[:,j0:j0+2,i0:i0+2]
            if not np.isfinite(h).all():raise DomainFail('MISSING_DATA')
            possible=np.flatnonzero((h.min(axis=(1,2))<=self.z)&(h.max(axis=(1,2))>=self.z))
            a,b=ip-i0,jp-j0
            for k in possible:
                f=h[int(k)];hp=P([float(f[0,0])-self.z])+(float(f[0,1])-float(f[0,0]))*a+(float(f[1,0])-float(f[0,0]))*b+(float(f[1,1])-float(f[1,0])-float(f[0,1])+float(f[0,0]))*a*b
                self.stats['polynomial_root_calls']+=1
                hp_roundoff=64*np.finfo(float).eps*max(1.,float(np.max(np.abs(hp.coef[1:]),initial=0.)))
                if any(left<c<right and abs(float(hp(c)))<=hp_roundoff for c in roots(hp.deriv(),left,right)):
                    raise BoundaryUnresolved('GRAZING_VERTICAL_EXTREMUM',dict(index=int(k)))
                for x in roots(hp,left,right):
                    if 0.<x<1. and abs(float(hp.deriv()(x)))<=POLICY['grazing_derivative_relative']*max(1e-30,float(np.max(np.abs(hp.coef[1:]),initial=0.))):
                        raise BoundaryUnresolved('GRAZING_VERTICAL_PARTITION',dict(index=int(k)))
                    candidates.append((x,'height',int(k)))
        return sorted(set(candidates))
    def bracket(self,dense,root,kind,index,neighbors):
        a,b=float(dense.t_old),float(dense.t);h=b-a
        lower=max([0.,*[x for x in neighbors if x<root]],default=0.);upper=min([1.,*[x for x in neighbors if x>root]],default=1.)
        lo=a+h*((lower+root)/2 if root>0 else 0.);hi=a+h*((root+upper)/2 if root<1 else 1.)
        if kind in ('i','j'):
            local=polynomials(dense)[0 if kind=='i' else 1]
            if not any(0.<c<1. for c in roots(local.deriv())):
                lo,hi=a,b # Actual monotone-map sign bracket; proxy never defines admission.
        f=lambda t:self.value(dense,t,kind,index)
        fl,fr=f(lo),f(hi)
        if fl==0. or fr==0.:
            # At an original switch/route endpoint the new segment starts there;
            # it is not silently advanced to a fictitious neighboring position.
            if root<=POLICY['root_endpoint_tolerance'] or root>=1-POLICY['root_endpoint_tolerance']:return None
            raise BoundaryUnresolved('GRAZING_OR_UNRESOLVED_ZERO_PLATEAU',dict(axis=kind,index=index,lo=lo,hi=hi))
        if fl*fr>=0:raise BoundaryUnresolved('TANGENTIAL_OR_UNISOLATED_EVENT',dict(axis=kind,index=index,lo=lo,hi=hi))
        before_sign=math.copysign(1.,fl)
        for _ in range(POLICY['root_iterations']):
            mid=(lo+hi)/2
            if mid in (lo,hi):break
            fm=f(mid)
            if fm==0.:
                # Keep the entire representable zero plateau, not just a zero
                # residual point incorrectly advertised as exact event time.
                left,right=mid,mid;lnz,rnz=lo,hi
                for _ in range(POLICY['root_iterations']):
                    m=(lnz+left)/2
                    if m in (lnz,left):break
                    v=f(m)
                    if v==0.:left=m
                    elif math.copysign(1.,v)==before_sign:lnz=m
                    else:raise BoundaryUnresolved('EVENT_ISOLATION_SIGN_CONFLICT')
                for _ in range(POLICY['root_iterations']):
                    m=(right+rnz)/2
                    if m in (right,rnz):break
                    v=f(m)
                    if v==0.:right=m
                    elif math.copysign(1.,v)!=before_sign:rnz=m
                    else:raise BoundaryUnresolved('EVENT_ISOLATION_SIGN_CONFLICT')
                lo,hi=lnz,rnz;break
            if math.copysign(1.,fm)==before_sign:lo=mid
            else:hi=mid
        if hi-lo>POLICY['max_bracket_width_s']:raise BoundaryUnresolved('EVENT_BRACKET_TOO_WIDE',dict(lo=lo,hi=hi))
        t={'LOWER':lo,'MID':(lo+hi)/2,'UPPER':hi}[self.choice]
        return dict(axis=kind,index=int(index),bracket_s=[lo,hi],width_s=hi-lo,t=float(t),direction=1 if fr>fl else -1,
            residual=float(f(t)),root_choice=self.choice,plateau_covered=True)
    def excluded_by_box(self,dense):
        # For x in [0,1], every x or (1-x) factor in DOP853's dense
        # recurrence has magnitude <=1, hence |s(x)-s(0)| <= sum|F|.
        # This is a containment filter, not a replacement physical C validator.
        self.stats['partition_box_probes']+=1
        if not all(hasattr(dense,k) for k in ['F','y_old']):raise BoundaryUnresolved('UNSUPPORTED_DENSE_BOX_SCHEMA')
        s=np.asarray(dense.y_old);variation=np.sum(np.abs(dense.F),axis=0)
        x=float(self.origin[0]+R0*s[0]);y=float(self.origin[1]+R0*s[1]);coord=self.raw.coordinates
        try:interp,i,j,_=self.raw._horizontal(x,y);heights=interp(self.raw.height)
        except AdapterError as exc:raise DomainFail(exc.status) from exc
        pad=64*np.finfo(float).eps*max(1.,abs(i),abs(j),coord.nx,coord.ny)
        di=abs(R0*coord.m0/coord.dx)*float(variation[0])+pad
        dj=abs(R0*coord.m0/coord.dy)*float(variation[1])+pad
        i0=min(max(math.floor(i),0),coord.nx-2);j0=min(max(math.floor(j),0),coord.ny-2)
        if i-di<=i0 or i+di>=i0+1 or j-dj<=j0 or j+dj>=j0+1:return False
        h=self.raw.height[:,j0:j0+2,i0:i0+2]
        if not np.isfinite(h).all():raise DomainFail('MISSING_DATA')
        li=np.maximum(np.abs(h[:,0,1]-h[:,0,0]),np.abs(h[:,1,1]-h[:,1,0]))
        lj=np.maximum(np.abs(h[:,1,0]-h[:,0,0]),np.abs(h[:,1,1]-h[:,0,1]))
        error=li*di+lj*dj+64*np.finfo(float).eps*np.maximum(1.,np.max(np.abs(h),axis=(1,2)))
        if np.any(np.abs(heights-self.z)<=error):return False
        self.stats['partition_box_exclusions']+=1
        return True

    def first_event(self,sol,start,end):
        for dense in sol.sol.interpolants:
            if self.excluded_by_box(dense):continue
            candidates=self.candidates(dense)
            for x,kind,index in candidates:
                t=float(dense.t_old+x*dense.h);eps=POLICY['initial_contact_ulps']*math.ulp(max(1.,abs(start)))
                last=self.last_event
                at_old=last and (kind,index)==(last['axis'],last['index']) and t<=last['bracket_s'][1]+eps and start>=last['bracket_s'][0]-eps
                if t<=start+eps or at_old:
                    self.contacts.append(dict(kind='INITIAL_OR_ALREADY_BRACKETED_CONTACT',axis=kind,index=int(index),t=t));continue
                if t>=end-POLICY['initial_contact_ulps']*math.ulp(max(1.,abs(end))):
                    self.contacts.append(dict(kind='ORIGINAL_CONTROL_OR_ROUTE_ENDPOINT_CONTACT',axis=kind,index=int(index),t=t));continue
                neighbors=[r[0] for r in candidates if r[1:]==(kind,index)]
                event=self.bracket(dense,x,kind,index,neighbors)
                if event is None:raise BoundaryUnresolved('UNRESOLVED_INTERIOR_ENDPOINT_CONTACT')
                if (last and (kind,index)==(last['axis'],last['index'])
                    and last['bracket_s'][0]-eps<=start<=last['bracket_s'][1]+eps
                    and event['bracket_s'][0]<=last['bracket_s'][1]+eps
                    and event['bracket_s'][1]>=last['bracket_s'][0]-eps):
                    # LOWER deliberately restarts on the incoming side of its
                    # enclosed zero plateau. This is the same event, not a new
                    # physical segment. Keep the union enclosure and evidence;
                    # never advance/snap the state or skip a disjoint bracket.
                    self.contacts.append(dict(kind='OVERLAPPING_ALREADY_ENCLOSED_EVENT',axis=kind,index=int(index),
                        t=event['t'],bracket_s=event['bracket_s']))
                    last['bracket_s']=[min(last['bracket_s'][0],event['bracket_s'][0]),max(last['bracket_s'][1],event['bracket_s'][1])]
                    last['width_s']=last['bracket_s'][1]-last['bracket_s'][0]
                    if last['width_s']>POLICY['max_bracket_width_s']:raise BoundaryUnresolved('EVENT_UNION_BRACKET_TOO_WIDE',last)
                    continue
                if event['t']<=start+eps:raise BoundaryUnresolved('NONADVANCING_EVENT',event)
                return event
        return None
    def integrate(self,a,b,state,u):
        start=float(a);current=np.asarray(state,float).copy();pieces=[]
        for _ in range(POLICY['max_events_per_run']+1):
            trial=stream_trial(self,start,b,current,u);event=trial.event
            if event is None:
                pieces.append(dict(a=start,b=float(b),sol=trial.sol,start_state=current.tolist(),end_state=trial.y[:,-1].tolist()));current=trial.y[:,-1];break
            if len(self.events)>=POLICY['max_events_per_run']:raise BoundaryUnresolved('BOUNDARY_EVENT_LIMIT')
            t=event['t'];prefix=self.solve(start,t,current,u,'committed_prefix')
            end_state=prefix.y[:,-1];event.update(state_before_restart=end_state.tolist(),state_after_restart=end_state.tolist(),
                context='OWN_PROVISIONAL_REPLAY_NOT_EXTERNAL_ORACLE',u=float(u))
            self.events.append(event);self.last_event=event
            self.stats['discarded_trial_tail_steps']+=int(np.sum(trial.t>t))
            pieces.append(dict(a=start,b=t,sol=prefix.sol,start_state=current.tolist(),end_state=end_state.tolist()))
            current=end_state;start=t
        else:raise BoundaryUnresolved('NONTERMINATING_BOUNDARY_REPLAY')
        self.pieces.extend(dict(a=p['a'],b=p['b'],start_state=p['start_state'],end_state=p['end_state']) for p in pieces)
        def dense(t):
            i=min(int(np.searchsorted([p['b'] for p in pieces],t,side='left')),len(pieces)-1)
            return pieces[i]['sol'](float(t))
        return current,dense


def stream_trial(engine,a,b,state,u):
    """Provisional trial only. An event-stopped return is NOT whole-route proof.

    Identical t_bound, initial-step choice, tolerance, max_step and dense calls as
    solve_ivp(DOP853,dense_output=True). Only future unused trial steps disappear.
    Committed prefix and every subsequent committed continuation remain required.
    """
    engine.stats['actual_ODE_calls']+=1
    engine.stats['trial_ODE_calls']+=1
    solver=DOP853(engine.rhs(u),float(a),np.asarray(state,float),float(b),
                  rtol=3e-14,atol=[1e-15]*3,max_step=engine.step)
    ts=[float(a)];ys=[np.asarray(state,float)];interpolants=[];event=None
    while solver.status=='running':
        solver.step()
        if solver.status=='failed':raise BoundaryUnresolved('ORDERED_INTEGRATION_FAILURE')
        dense=solver.dense_output()
        ts.append(solver.t);ys.append(solver.y);interpolants.append(dense)
        if engine.raw is not None:
            # Reuse the exact original detector, using the ORIGINAL trial bounds.
            # Detection never uses a reference event list or an endpoint-only test.
            one=SimpleNamespace(sol=SimpleNamespace(interpolants=[dense]))
            event=engine.first_event(one,float(a),float(b))
            if event is not None:break
    if not interpolants:raise BoundaryUnresolved('EMPTY_STREAMED_TRIAL')
    engine.stats['RHS_evaluations']+=int(solver.nfev)
    engine.stats['accepted_trial_steps']+=len(ts)-1
    return SimpleNamespace(t=np.asarray(ts),y=np.asarray(ys).T,
        sol=OdeSolution(np.asarray(ts),interpolants),success=True,nfev=int(solver.nfev),
        event=event,completed_interval=(event is None and solver.status=='finished'),
        scope='PROVISIONAL_TRIAL_NOT_ACCEPTANCE')
