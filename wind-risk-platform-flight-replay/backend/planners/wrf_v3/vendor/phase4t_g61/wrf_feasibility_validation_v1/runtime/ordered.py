"""Independent displaced-coordinate ordered replay with a conservative numerical gate.

This is nested-discretization engineering evidence, NOT interval arithmetic or
a rigorous ODE error certificate. No shooting implementation is imported.
"""
import math
from fractions import Fraction
import numpy as np
from scipy.integrate import solve_ivp
from phase3a_implementation.domain import VA,R0
from phase3b.validator import THRESHOLDS
from phase3a_implementation.adapter import StaticWRFWindAdapter
from phase4e.acceleration.wind import ExactCachedWRFWindProvider
from phase4t.amendment_v1.model import StructuralStop,DomainFail
from phase4a_design.mock_contract import content_hash
from .boundaries import PartitionReplay,BoundaryUnresolved,POLICY as EVENT_POLICY

POLICY=dict(version='LOCAL_ORDERED_STREAMED_BOUNDARY_REPLAY_V1',method='INDEPENDENT_DISPLACED_DOP853',
    rtol=3e-14,atol_scaled=[1e-15,1e-15,1e-15],max_steps_s=[.02,.01],
    uncertainty_multiplier=16.,global_roundoff_ulp_multiplier=8.,
    plane_threshold_m=1e-9,progress_threshold_s=1e-9,segment_heading_rad=math.pi,
    result_states=['PASS','FAIL','NUMERICAL_UNRESOLVED'],
    numerical_assurance='EMPIRICAL_NESTED_DISCRETIZATION_ENVELOPE_NOT_RIGOROUS_INTERVAL_CERTIFICATE',
    no_controller_progress_flags=True,all_original_segments_preserved=True,
    coalescing='INTEGRATION_ONLY_ADJACENT_BIT_IDENTICAL_CONTROLS',
    original_control_rows_and_segment_events_retained=True,
    cumulative_time='EXACT_SUM_OF_BINARY64_DURATIONS_CONVERTED_TO_FLOAT',
    boundary_events=dict(EVENT_POLICY),independent_event_sensitivity_replays=True)

def fresh(provider):
    if isinstance(provider,ExactCachedWRFWindProvider):provider=provider.reference
    return ExactCachedWRFWindProvider(provider) if isinstance(provider,StaticWRFWindAdapter) else provider

class PathReplay:
    def __init__(self,tr,provider,step,*,event_choice="MID"):
        self.tr=tr;self.provider=fresh(provider);self.origin=np.array(tr['state_start'],float);self.z=tr['z_layer_msl_m']
        ds=tr['durations'];exact=Fraction(0);ends=[];groups=[];mapping=[]
        for u,d in zip(tr['controls'],ds):
            a=exact;exact+=Fraction(float(d));ends.append(float(exact))
            if groups and float(u).hex()==float(groups[-1][2]).hex():groups[-1]=(groups[-1][0],float(exact),u)
            else:groups.append((float(a),float(exact),u))
            mapping.append(len(groups)-1)
        self.ends=np.array(ends);self.starts=np.r_[0.,self.ends[:-1]];self.sol=[]
        self.integration_run_count=len(groups);self.original_control_row_count=len(ds)
        self.step=step
        self.engine=PartitionReplay(self.provider,self.origin,self.z,step,choice=event_choice)
        state=np.zeros(3);run_solutions=[]
        for a,b,u in groups:
            if b<=a:raise ValueError('NONPOSITIVE_ORDERED_DURATION')
            state,dense=self.engine.integrate(a,b,state,float(u));run_solutions.append(dense)
        self.sol=[run_solutions[i] for i in mapping]
    def local(self,i,t):
        s=self.sol[i](float(np.clip(t,self.starts[i],self.ends[i])))
        return np.array([R0*s[0],R0*s[1],s[2]])
    def local_at(self,t):
        i=min(int(np.searchsorted(self.ends,t,side='left')),len(self.ends)-1)
        return self.local(i,t)
    def geometry(self,i,t):
        q=self.local(i,t);x=self.origin[0]+q[0];y=self.origin[1]+q[1];psi=self.origin[2]+q[2]
        w=self.provider.wind_query(float(x),float(y),float(self.z),max(0.,float(t)))
        if not w.get('valid') or math.hypot(w['wx'],w['wy'])>=VA:raise DomainFail('ORDERED_GEOMETRY_DOMAIN')
        v=np.array([VA*math.cos(psi)+w['wx'],VA*math.sin(psi)+w['wy']]);speed=float(np.linalg.norm(v))
        if speed<=0:raise ValueError('ZERO_GROUND_SPEED')
        return q,v,v/speed

def tape_audit(actual,planned):
    """Compare recorded controls and exact binary-float durations, no controller flags."""
    def cells(tr):
        t=Fraction(0);out=[]
        for u,d in zip(tr['controls'],tr['durations']):
            end=t+Fraction(float(d));out.append((t,end,Fraction(float(u))));t=end
        return out,t
    aa,ta=cells(actual);bb,tb=cells(planned);i=j=0;area=Fraction(0)
    while i<len(aa) and j<len(bb):
        lo=max(aa[i][0],bb[j][0]);hi=min(aa[i][1],bb[j][1])
        if hi>lo:area+=(hi-lo)*abs(aa[i][2]-bb[j][2])
        ae,be=aa[i][1],bb[j][1]
        if ae<=be:i+=1
        if be<=ae:j+=1
    return dict(control_L1_difference_rad=float(area),total_duration_difference_s=float(ta-tb),
        actual_control_count=len(aa),planned_segment_count=len(bb),
        comparison='EXACT_FRACTION_OF_RECORDED_FLOATS',not_a_standalone_geometry_certificate=True)

def progress(actual,reference,times,position_uncertainty,heading_uncertainty):
    i=0;s=0.;last=np.zeros(2);last_t=0.;events=[];trace=[];last_predicate=None
    for t in times:
        q=actual.local_at(t);xy=q[:2];psi=actual.origin[2]+q[2];dt=t-last_t
        r,v,_=reference.geometry(i,s);candidate=s+float((xy-last)@v/(v@v))
        lo=max(float(reference.starts[i]),s-max(.1,2*dt));hi=min(float(reference.ends[i]),s+max(.1,2*dt))
        candidate=float(np.clip(candidate,lo,hi))
        for _ in range(6):
            rr,vv,_=reference.geometry(i,candidate)
            candidate=float(np.clip(candidate+float((xy-rr[:2])@vv/(vv@vv)),lo,hi))
        s=candidate;end=float(reference.ends[i]);r,v,d=reference.geometry(i,end)
        plane=float((xy-r[:2])@d);head=float(abs(psi-reference.origin[2]-r[2]));speed=float(np.linalg.norm(v))
        # Local inverse-projection conditioning estimate; explicitly numerical,
        # not claimed as a rigorous reachable-set or global inverse-map bound.
        time_uncertainty=2*position_uncertainty/speed
        predicate=dict(segment=i,t=float(t),parameter_s=s,end_parameter_s=end,plane_m=plane,
            plane_interval_m=[plane-position_uncertainty,plane+position_uncertainty],
            parameter_interval_s=[s-time_uncertainty,s+time_uncertainty],heading_error_rad=head,
            plane_pass=bool(plane-position_uncertainty>=-POLICY['plane_threshold_m']),
            progress_pass=bool(s-time_uncertainty>=end-POLICY['progress_threshold_s']),
            heading_pass=bool(head+heading_uncertainty<=POLICY['segment_heading_rad']))
        last_predicate=predicate
        if predicate['plane_pass'] and predicate['progress_pass'] and predicate['heading_pass'] and len(events)==i:
            events.append(predicate)
            if i<len(reference.ends)-1:i+=1;s=float(reference.starts[i])
        trace.append(dict(t=float(t),segment=i,reference_parameter_s=s));last=xy;last_t=t
    complete=len(events)==len(reference.ends)
    if complete:status='PASS'
    elif last_predicate and (last_predicate['plane_interval_m'][1]<-POLICY['plane_threshold_m'] or
            last_predicate['parameter_interval_s'][1]<last_predicate['end_parameter_s']-POLICY['progress_threshold_s'] or
            last_predicate['heading_error_rad']-heading_uncertainty>POLICY['segment_heading_rad']):status='FAIL'
    else:status='NUMERICAL_UNRESOLVED'
    return dict(status=status,complete=complete,completed_segments=len(events),original_segment_count=len(reference.ends),
        events=events,last_predicate=last_predicate,trace=trace,final_reference_parameter_s=s)

def assess(c,actual,reference):
    planned=reference.tr;reference.route.verify()
    for key in ['state_start','state_goal','frame_id','height_datum','z_layer_msl_m','parameters']:
        if actual.get(key)!=planned.get(key):raise StructuralStop('ORDERED_ROUTE_IDENTITY:'+key)
    result=dict(method=POLICY['version'],policy=dict(POLICY),complete=False,status='FAIL',
        original_segment_count=len(planned['durations']),completed_segments=0,
        trust_controller_progress=False,actual_controls_sha256=content_hash(dict(controls=actual['controls'],durations=actual['durations'])),
        planned_route_sha256=reference.route.sha256,tape_audit=tape_audit(actual,planned))
    if not c['valid']:return dict(result,reason='PHYSICAL_C_NOT_PASSED')
    if abs(c['diagnostic_final_state'][2]-planned['state_goal'][2])>THRESHOLDS['heading_rad']:
        return dict(result,reason='ORIGINAL_UNWRAPPED_HEADING_MISMATCH')
    levels=[]
    for step in POLICY['max_steps_s']:
        levels.append((PathReplay(actual,reference.provider,step),PathReplay(planned,reference.provider,step)))
    ac,rc=levels[0];af,rf=levels[1]
    # Union of independent C sample times, all actual switch endpoints and dense
    # .02s probes; no stored trajectory positions are used as ODE reset states.
    total=float(af.ends[-1]);times=sorted(set([0.,total,*[min(total,max(0.,float(r['t']))) for r in c['wind_history']],
        *[float(t) for t in af.ends],*[float(t) for t in np.linspace(0,total,max(2,math.ceil(total/.02)+1))]]))
    ref_times=sorted(set([0.,float(rf.ends[-1]),*[float(t) for t in rf.ends],
        *[float(t) for t in np.linspace(0,float(rf.ends[-1]),max(2,math.ceil(float(rf.ends[-1])/.02)+1))]]))
    ad=max(float(np.linalg.norm(af.local_at(t)[:2]-ac.local_at(t)[:2])) for t in times)
    rd=max(float(np.linalg.norm(rf.local_at(t)[:2]-rc.local_at(t)[:2])) for t in ref_times)
    hd=max([abs(af.local_at(t)[2]-ac.local_at(t)[2]) for t in times]+[abs(rf.local_at(t)[2]-rc.local_at(t)[2]) for t in ref_times])
    scale=max(1.,float(np.max(np.abs(rf.origin[:2]))));ulp=math.ulp(scale)
    roundoff=POLICY['global_roundoff_ulp_multiplier']*ulp
    # Independent endpoint-of-bracket replays for every path that encountered
    # events. They detect their own events with fresh query providers; no event
    # lists or ODE outputs are passed between actual/reference or nested levels.
    sensitivities={'actual':0.,'reference':0.};extra=[]
    for level,pair in enumerate(levels):
        for role,primary,probes,tr in [('actual',pair[0],times,actual),('reference',pair[1],ref_times,planned)]:
            if not primary.engine.events:continue
            for choice in ['LOWER','UPPER']:
                alternative=PathReplay(tr,reference.provider,POLICY['max_steps_s'][level],event_choice=choice)
                delta=max(float(np.linalg.norm(primary.local_at(t)[:2]-alternative.local_at(t)[:2])) for t in probes)
                sensitivities[role]=max(sensitivities[role],delta)
                extra.append(dict(role=role,level=level,choice=choice,delta_m=delta,events=alternative.engine.events,
                    stats=alternative.engine.stats,continuous_pieces=alternative.engine.pieces))
    uncertainty=POLICY['uncertainty_multiplier']*(ad+rd+sensitivities['actual']+sensitivities['reference'])+roundoff
    heading_uncertainty=float(POLICY['uncertainty_multiplier']*hd+32*np.finfo(float).eps*max(1.,abs(rf.origin[2])))
    gate=progress(af,rf,times,uncertainty,heading_uncertainty)
    return dict(result,**{k:v for k,v in gate.items() if k not in result},
        **{k:gate[k] for k in ['status','complete','completed_segments']},
        replay_representation=dict(actual_control_rows=af.original_control_row_count,actual_integration_runs=af.integration_run_count,
            planned_original_segments=rf.original_control_row_count,planned_integration_runs=rf.integration_run_count,
            all_original_switch_times_probed=True,controls_or_durations_deleted=False),
        boundary_replays=dict(primary=[dict(level=i,role=role,events=p.engine.events,contacts=p.engine.contacts,
            stats=p.engine.stats,continuous_pieces=p.engine.pieces) for i,pair in enumerate(levels) for role,p in zip(['actual','reference'],pair)],
            event_sensitivity_replays=extra,event_sensitivity_m=sensitivities,no_shared_event_lists=True,
            empirical_not_rigorous_event_bound=True),
        uncertainty=dict(actual_event_sensitivity_m=sensitivities['actual'],reference_event_sensitivity_m=sensitivities['reference'],actual_step_delta_m=ad,reference_step_delta_m=rd,coordinate_roundoff_m=roundoff,
            position_envelope_m=uncertainty,heading_envelope_rad=heading_uncertainty,
            scope=POLICY['numerical_assurance']),reason='ALL_ORIGINAL_ORDERED_PREDICATES_WITHIN_ESTIMATED_ENVELOPE' if gate['complete'] else 'ORDERED_PREDICATES_NOT_RESOLVED_OR_FAILED')


# Ambiguous geometry/root isolation is an ordinary conservative numerical limit,
# never an accepted ordered route and never a fabricated physical infeasibility.
_assess=assess
def assess(c,actual,reference):
    try:return _assess(c,actual,reference)
    except BoundaryUnresolved as exc:
        return dict(method=POLICY['version'],policy=dict(POLICY),complete=False,status='NUMERICAL_UNRESOLVED',
            original_segment_count=len(reference.tr['durations']),completed_segments=0,
            reason='BOUNDARY_EVENT_NUMERICAL_UNRESOLVED',detail=exc.reason,evidence=exc.evidence,
            actual_controls_sha256=content_hash(dict(controls=actual['controls'],durations=actual['durations'])),
            planned_route_sha256=reference.route.sha256,no_controller_progress_flags=True)
