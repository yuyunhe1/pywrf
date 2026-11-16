"""Exact frozen input checks and typed rejection, with unchanged physical limits."""
import copy
import math
import time
from pathlib import Path
import numpy as np
from .common import DESIGN,read,sha,content_hash,StructuralStop
from phase3a_implementation.domain import VA,R0
from phase3b.validator import THRESHOLDS
from phase4t.amendment_v1.model import goal_errors

PT=R0*THRESHOLDS['position_over_R0'];HT=THRESHOLDS['heading_rad'];UMAX=VA/R0
H=8.;PREPARE=8.;TICK=.02

POLICIES={'R0_LEGACY_SELECTION':'FIRST_COLLISION_VALID','R1_LIFT_AWARE_SELECTION':'FIRST_LIFT_C_COLLISION_VALID'}

def validate_request(req):
    manifest=read(Path(__file__).parent/'EXECUTION_MANIFEST.json')
    if req.get('design_manifest_sha256')!=sha(Path(__file__).parent/'EXECUTION_MANIFEST.json'):raise StructuralStop('IDENTITY_DESIGN_MANIFEST')
    m=req.get('mission')
    if not isinstance(m,dict) or not any(m==v for v in manifest['missions']):raise StructuralStop('IDENTITY_MISSION_OR_ACTUAL_STATE')
    if req.get('arm') not in POLICIES or req.get('candidate_policy')!=POLICIES[req['arm']]:raise StructuralStop('CANDIDATE_POLICY_IDENTITY')
    if not any(r['run_id']==req.get('run_id') and r['mission']==m['id'] and r['arm']==req['arm'] and r['prefix_policy']==m['prefix_policy']['name'] for r in manifest['serial_order']):raise StructuralStop('IDENTITY_RUN_ORDER')
    if not isinstance(req.get('nonce'),str) or not req['nonce'] or not isinstance(req.get('task_epoch'),str):raise StructuralStop('IPC_INVALID_NONCE')
    if req.get('expected_ordered_pool_sha256') is not None:raise StructuralStop('NO_PRECOMPUTED_POOL_AUTHORIZED')
    if req.get('physical_deadline_s')!=m['absolute_physical_deadline_s']:raise StructuralStop('PHYSICAL_DEADLINE_RESET')
    if req.get('prepare_deadline_perf')!=req['owner_start_perf']+PREPARE:raise StructuralStop('PREPARE_DEADLINE_CHANGED')
    if req.get('owner_deadline_perf')!=req['owner_start_perf']+120.:raise StructuralStop('TASK_DEADLINE_CHANGED')
    if not all(isinstance(req[k],(int,float)) and not isinstance(req[k],bool) and math.isfinite(req[k]) for k in ('owner_start_perf','owner_deadline_perf','prepare_deadline_perf','physical_deadline_s')):raise StructuralStop('NONFINITE_CLOCK')
    if req.get('test_fault') is not None:raise StructuralStop('TEST_FAULT_IN_FORMAL_REQUEST')
    if (Path(__file__).parent/'RUNTIME_FREEZE.json').exists():
        frozen=read(Path(__file__).parent/'RUNTIME_FREEZE.json')
        if req.get('runtime_freeze_sha256')!=sha(Path(__file__).parent/'RUNTIME_FREEZE.json') or req['task_epoch']!=frozen['epoch']:raise StructuralStop('RUNTIME_FREEZE_REQUEST_BINDING')
        if not any(r['run_id']==req['run_id'] and r['nonce']==req['nonce'] and r['arm']==req['arm'] for r in frozen['order']):raise StructuralStop('FROZEN_NONCE_OR_ARM')
    tr=m['original_route']
    if content_hash(tr)!=m['original_route_sha256']:raise StructuralStop('ORIGINAL_ROUTE_HASH')
    if tr['state_goal']!=m['original_goal'] or m['original_goal'][2]!=m['original_goal_unwrapped_psi_air']:raise StructuralStop('ORIGINAL_GOAL_LIFT_BINDING')
    if m['T_ref_s']!=math.fsum(tr['durations']) or m['absolute_physical_deadline_s']!=2*m['T_ref_s']+10.:raise StructuralStop('ORIGINAL_HORIZON_BINDING')
    if m['trigger']!={'kind':'SCHEDULED_AT_ACTUAL_PLANT_INITIALIZATION','physical_time_s':0.,'uses_manual_observation':False}:raise StructuralStop('UNFROZEN_TRIGGER')
    if m['prefix_for_B']['duration_s']!=H or m['prefix_for_B']['tick_s']!=TICK:raise StructuralStop('PREFIX_CONTRACT')
    from .prefix import schedule
    if m['prefix_policy']['name'] not in ('REFERENCE_SCHEDULE_8S','SATURATED_FIRST5_THEN_REFERENCE_3S') or math.fsum(m['prefix_policy']['durations'])!=H:raise StructuralStop('PREFIX_POLICY_IDENTITY')
    if len(m['prefix_policy']['controls'])!=len(m['prefix_policy']['durations']) or any(d<=0 for d in m['prefix_policy']['durations']) or any(abs(u)>UMAX+1e-12 for u in m['prefix_policy']['controls']):raise StructuralStop('PREFIX_POLICY_BOUNDS')
    from .integrity import verify_sources
    if req.get('startup_variant') not in ('LEAN_SERIAL','LEAN_OVERLAP') or req.get('source_guard_policy')!='REQUEST_SEAL_ONLY':
        raise StructuralStop('STARTUP_POLICY_NOT_FROZEN')
    if (Path(__file__).parent/'RUNTIME_FREEZE.json').exists():
        if not any(r['run_id']==req['run_id'] and r.get('startup_variant')==req['startup_variant'] and r.get('source_guard_policy')==req['source_guard_policy'] for r in frozen['order']):
            raise StructuralStop('STARTUP_VARIANT_IDENTITY')
    verify_sources(require_freeze=False,policy=req['source_guard_policy'])
    return True

def check_actual(tr,req):
    m=req['mission'];orig=m['original_route']
    for k,v in orig.items():
        if k not in ('state_start','states','controls','durations') and tr.get(k)!=v:raise StructuralStop('ACTUAL_IMMUTABLE_FIELD:'+k)
    if tr.get('state_start')!=m['actual_initial_state']:raise StructuralStop('ACTUAL_ORIGIN_SUBSTITUTION')
    try:
        rawvalues=[*tr['controls'],*tr['durations'],*(v for row in tr['states'] for v in row)]
        if any(isinstance(v,(bool,np.bool_)) for v in rawvalues):raise ValueError('boolean numeric field')
        us=np.asarray(tr['controls'],float);ds=np.asarray(tr['durations'],float);qs=np.asarray(tr['states'],float)
        if us.ndim!=1 or us.shape!=ds.shape or qs.shape!=(len(us)+1,3) or not np.isfinite(np.r_[us,ds,qs.ravel()]).all() or np.any(ds<=0):raise ValueError('shape/finiteness')
        if not np.array_equal(qs[0],m['actual_initial_state']):raise ValueError('initial')
    except Exception as exc:raise StructuralStop('ACTUAL_CONTROL_STATE_SCHEMA:'+str(exc))
    if np.max(np.abs(us),initial=0.)>UMAX+1e-12:raise StructuralStop('CONTROL_BOUND')
    total=math.fsum(ds)
    if total>req['physical_deadline_s']:raise StructuralStop('PHYSICAL_HORIZON_EXCEEDED')
    return total

def lift_ok(psi,goal):
    return abs(psi-goal)<=math.pi and abs(math.atan2(math.sin(psi-goal),math.cos(psi-goal)))<=HT

def join_ok(actual,predicted):
    p,h=goal_errors(actual,predicted)
    return bool(p<=PT and h<=HT and abs(actual[2]-predicted[2])<=math.pi),dict(position_m=p,wrapped_heading_rad=h,unwrapped_heading_rad=float(actual[2]-predicted[2]))

def candidate_status(c,collision,req,total):
    if total>req['physical_deadline_s']:return 'REPLAN_PHYSICAL_HORIZON_EXHAUSTED'
    if c['domain_status']!='IN_DOMAIN_AT_EVALUATED_POINTS':return 'REPLAN_DOMAIN_REJECT'
    if any(f in ('INTERIOR_NODE_RESIDUAL','CONTROL_BOUND','INVALID_INPUT') for f in c['failures']):raise StructuralStop('C_CANDIDATE_STATE_MISMATCH:'+str(c['failures']))
    if not collision['valid']:return 'REPLAN_COLLISION_REJECT'
    if c['position_error']>PT:return 'REPLAN_ENDPOINT_MISMATCH'
    psi=c['diagnostic_final_state'][2]
    if not lift_ok(psi,req['mission']['original_goal_unwrapped_psi_air']):return 'REPLAN_HEADING_MISMATCH'
    if not c['valid']:return 'REPLAN_NO_VALID_ROUTE'
    return 'PREPARED_ROUTE_VALID'

def full_route(req,predicted,suffix,prefix_nodes):
    m=req['mission'];tr=copy.deepcopy(m['original_route']);q0=m['actual_initial_state']
    if suffix.get('state_start')!=list(predicted) or suffix.get('state_goal')!=m['original_goal']:raise StructuralStop('SOLVER_BOUNDARY_IDENTITY')
    for k in ('parameters','frame_id','height_datum','z_layer_msl_m','type','control_interpolation'):
        if suffix.get(k)!=m['original_route'][k]:raise StructuralStop('SOLVER_CONTEXT_SCHEMA:'+k)
    prefix=m['prefix_policy'];nodes=prefix_nodes
    if nodes[0]!=q0 or nodes[-1]!=list(predicted):raise StructuralStop('PREFIX_NODE_IDENTITY')
    tr.update(state_start=q0,controls=prefix['controls']+suffix['controls'],durations=prefix['durations']+suffix['durations'],
        states=[*nodes,*suffix['states'][1:]])
    # Zero durations remain in the raw candidate. No segments are silently deleted.
    if any(d<=0 for d in tr['durations']):return tr,'ZERO_DURATION_REFERENCE_UNSUPPORTED_BY_INHERITED_PLANNEDROUTE'
    return tr,None

def execution_binding(req):
    return dict(version='G5_ACTUAL_CONTROLS_BINDING_V1',run_id=req['run_id'],task_epoch=req['task_epoch'],nonce=req['nonce'],
        original_route_sha256=req['mission']['original_route_sha256'],actual_initial_state=req['mission']['actual_initial_state'],
        original_goal=req['mission']['original_goal'],context_id=req['mission']['original_context_id'],
        physical_deadline_s=req['physical_deadline_s'],request_sha256=content_hash(req))

def safety_bound(m,ctx=None):
    manifest=read(Path(__file__).parent/'EXECUTION_MANIFEST.json')
    if not any(m==v for v in manifest['missions']):raise StructuralStop('UNREGISTERED_G61_CONTEXT')
    if m['provider_spec']['kind'] not in ('WRF_STATIC_SNAPSHOT','SYNTHETIC_CONSTANT'):raise StructuralStop('UNREGISTERED_PROVIDER_KIND')
    if ctx is None:raise StructuralStop('MISSING_LIVE_CONTEXT')
    probes=[]
    for name,q in [('actual_start',m['actual_initial_state']),('original_goal',m['original_goal'])]:
        sample=ctx.provider.wind_query(q[0],q[1],m['z_layer_msl_m'],0.)
        beta=math.hypot(sample['wx'],sample['wy'])/VA if sample.get('valid') else None
        probes.append(dict(location=name,sample=sample,beta=beta))
    return dict(domain_pass=all(p['sample'].get('valid') and p['beta'] is not None and p['beta']<1. for p in probes),probes=probes,
        fixed_horizon_any_bounded_control_ROI_safe='NOT_PROVEN_NOT_REQUIRED',scope='ENDPOINTS_ONLY_CONTINUOUS_PREFIX_AND_ROUTE_GATES_STILL_REQUIRED')
