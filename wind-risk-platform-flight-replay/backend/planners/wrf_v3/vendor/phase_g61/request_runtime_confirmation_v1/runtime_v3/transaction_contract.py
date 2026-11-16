"""Two bounded groups; each request binds actual history and its accepted reference.

Global transaction numbers are unique. Local attempt numbers belong to one
fixed eight-second group; cancellation never renews that group's deadline.
"""
import copy
import math
from pathlib import Path
from .work import read,sha,content
from .window import Window

MAX_GROUPS=2
MAX_ATTEMPTS_PER_GROUP=2


def prefix_controls(route,duration=8.):
    if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<=0 or duration>8.:
        raise ValueError('CAUSAL_PREFIX_DURATION')
    us=[];ds=[]
    for u,d in zip(route['controls'],route['durations']):
        left=duration-math.fsum(ds)
        if left<=0:break
        us.append(float(u));ds.append(min(float(d),left))
    left=duration-math.fsum(ds)
    if left>0:
        if not us:raise ValueError('NO_NOMINAL_CONTROL_FOR_REMAINING_PREFIX')
        us.append(us[-1]);ds.append(left)
    return dict(name='COMMITTED_SCHEDULE_THEN_LAST_HOLD_CAUSAL_REMAINING',controls=us,durations=ds,duration_s=float(duration))


def group(req,root,base,common):
    path=(base/req['group_origin_file']).resolve()
    if not path.is_relative_to(base.resolve()) or sha(path)!=req['group_origin_sha256']:raise ValueError('GROUP_ORIGIN_PATH_OR_HASH')
    origin=common.unpack(root,'PREPARE_ORIGIN',read(path));g=req['group_index']
    if type(g) is not int or not 1<=g<=MAX_GROUPS or origin['group_index']!=g or origin['reference_index']!=g-1:
        raise ValueError('PREPARATION_GROUP_LIMIT_OR_REFERENCE')
    if path!=(base/'groups'/f'{g:02d}'/'origin.json').resolve():raise ValueError('GROUP_ORIGIN_CANONICAL_PATH')
    w=Window.begin(origin['physical_t'],origin['trigger_perf'],root['owner_deadline'],root['case']['physical_deadline_s'])
    if origin['physical_handover_s']!=w.physical_handover or origin['prepare_deadline']!=w.first_deadline:
        raise ValueError('ORIGINAL_PREPARATION_WINDOW_CHANGED')
    return origin,w


def verify_clock(req,root,anchor):
    from .engine.runtime import common
    base=Path(req['root_request_path']).parent;origin,w=group(req,root,base,common)
    observation=anchor['observation_perf'];expected=w.at(anchor['physical_t'],observation)
    checks=[(req['owner_start']==origin['trigger_perf'],'FIRST_TRIGGER_WALL_RESET'),
        (anchor['trigger_perf']==origin['trigger_perf'],'ANCHOR_FIRST_TRIGGER_CHANGED'),
        (req['prepare_deadline']==expected['prepare_deadline'],'RESTART_DEADLINE_EXTENDED_OR_CHANGED'),
        (req['prefix_duration_s']==expected['prefix_duration_s'],'RESTART_PHYSICAL_WINDOW_CHANGED'),
        (req['physical_handover_s']==w.physical_handover,'PHYSICAL_HANDOVER_RESET'),
        (req['attempt_observation_perf']==observation,'OBSERVATION_TIME_BINDING'),
        (req['owner_deadline']==root['owner_deadline'],'GLOBAL_WALL_RESET'),
        (root['owner_deadline']==root['owner_start']+120.,'ROOT_120S_CHANGED'),
        (req['case']['global_physical_offset_s']==anchor['physical_t'],'WIND_CLOCK_RESET'),
        (req['case']['physical_deadline_s']==root['case']['physical_deadline_s']-anchor['physical_t'],'GLOBAL_PHYSICAL_BUDGET_RESET'),
        (req['case']['T_ref_s']==root['case']['T_ref_s'],'IMMUTABLE_TREF_CHANGED')]
    for okay,label in checks:
        if not okay:raise ValueError(label)


def prior_input_paths(req,root,base,common):
    """Immutable ancestor bytes included in the existing early/final ACK reads."""
    from .engine.contract import within
    paths=[]
    for n in range(1,req['transaction_index']):
        tx=base/'transactions'/f'{n:02d}'
        for name in ['request.json','anchor.json','actual_prefix.json','prepared.json','prepared_ack.json',
                     'commit.json','admission.json','parent_validation.json','prefix_assessment.json',
                     'solver_result.json','invalidation.json','anchor_snapshot/prefix_identity.json','anchor_snapshot/prefix.jsonl']:
            p=tx/name
            if p.is_file():paths.append(p)
        if (tx/'request.json').exists():
            previous=read(tx/'request.json');paths.append(within(base,previous['group_origin_file']))
        if (tx/'anchor.json').exists():
            anchor=common.unpack(root,'ACTUAL_ANCHOR',read(tx/'anchor.json'));paths.append(within(base,anchor['reference_file']))
    return paths


def previous_reference(req,root,base,ref,actual,anchor,visited):
    from .engine.runtime import common
    from phase3b.validator import THRESHOLDS
    from phase4t_g61.wrf_feasibility_validation_v1.runtime.contract import join_ok
    previous=req.get('previous_committed_transaction_index');g=req['group_index'];n=req['transaction_index']
    if g==1:
        if previous is not None or ref['reference_index']!=0 or ref['origin_physical_s']!=0. or content(ref['trajectory'])!=content(root['case']['route']):
            raise ValueError('ORIGINAL_REFERENCE_CHANGED')
        return 0
    if type(previous) is not int or not 1<=previous<n:raise ValueError('PREVIOUS_COMMIT_INDEX')
    prior=base/'transactions'/f'{previous:02d}';preq=read(prior/'request.json')
    verify_transaction(preq,_visited=visited)
    if preq['group_index']!=g-1 or preq['root_request_sha256']!=req['root_request_sha256']:
        raise ValueError('PREVIOUS_COMMIT_GROUP_OR_ROOT')
    if (prior/'invalidation.json').exists():raise ValueError('PREVIOUS_COMMIT_INVALIDATED')
    prepared_packet=read(prior/'prepared.json');p=common.unpack(preq,'PREPARED_ROUTE',prepared_packet)
    commit=common.unpack(preq,'COMMIT',read(prior/'commit.json'))
    ack=common.unpack(preq,'PREPARED_ACK',read(prior/'prepared_ack.json'))
    admission=common.unpack(preq,'TX_ADMISSION',read(prior/'admission.json'))
    parent=read(prior/'parent_validation.json');prefix=read(prior/'prefix_assessment.json')
    if (admission.get('accepted') is not True or admission.get('durable_before_prepare_deadline') is not True or
        admission.get('transaction_index')!=previous or admission.get('group_index')!=g-1 or
        admission['intent_perf']!=commit['commit_perf'] or admission['prepare_inclusive_s']>=8. or
        not preq['owner_start']<=admission['persisted_readback_perf']<preq['prepare_deadline'] or
        not preq['owner_start']<=commit['commit_perf']<preq['prepare_deadline'] or
        commit['prepared_sha256']!=prepared_packet['sha256'] or ack.get('accepted') is not True or
        ack['prepared_sha256']!=prepared_packet['sha256'] or not preq['owner_start']<=ack['observed_perf']<preq['prepare_deadline'] or
        not preq['owner_start']<=p['published_perf']<preq['prepare_deadline']):
        raise ValueError('PREVIOUS_COMMIT_NOT_COMPLETE_AND_TIMELY')
    if admission['persisted_readback_perf']>anchor['observation_perf']:raise ValueError('REFERENCE_FROM_FUTURE_COMMIT')
    tr=p['trajectory'];c=parent['C'];co=parent['collision']
    if (p['full_validation_sha256']!=sha(prior/'parent_validation.json') or p['prefix_assessment_sha256']!=sha(prior/'prefix_assessment.json') or
        parent['request_sha256']!=content(preq) or p['context_id']!=preq['parent_context_id'] or parent['context_id']!=p['context_id'] or
        parent['trajectory_sha256']!=content(tr) or p['trajectory_sha256']!=content(tr)):
        raise ValueError('PREVIOUS_ROUTE_RECEIPT_BINDING')
    if (c.get('valid') is not True or co.get('valid') is not True or c['thresholds']!=THRESHOLDS or
        not prefix['collision']['valid'] or not prefix['prefix_dynamics_pass'] or
        abs(c['diagnostic_final_state'][2]-root['case']['route']['state_goal'][2])>THRESHOLDS['heading_rad'] or
        math.hypot(c['diagnostic_final_state'][0]-tr['state_goal'][0],c['diagnostic_final_state'][1]-tr['state_goal'][1])>8.513422179807296e-6):
        raise ValueError('PREVIOUS_ROUTE_C_COLLISION_OR_RAW_LIFT')
    if (ref['committed_transaction_index']!=previous or ref['source_prepared_sha256']!=prepared_packet['sha256'] or
        ref['origin_physical_s']!=preq['case']['global_physical_offset_s'] or content(ref['trajectory'])!=content(tr) or
        tr['state_start']!=preq['case']['route']['state_start'] or tr['state_goal']!=root['case']['route']['state_goal']):
        raise ValueError('CURRENT_REFERENCE_NOT_PREVIOUS_ACCEPTED_ROUTE')
    previous_actual=read(prior/'actual_prefix.json')['trajectory'];length=len(previous_actual['controls'])
    if any(actual[k][:length]!=previous_actual[k] for k in ['controls','durations']) or actual['states'][:length+1]!=previous_actual['states']:
        raise ValueError('GLOBAL_ACTUAL_HISTORY_REWRITTEN_AFTER_COMMIT')
    if not join_ok(p['actual_handover_state'],p['predicted_handover_state'])[0] or commit['actual_handover_state']!=p['actual_handover_state']:
        raise ValueError('PREVIOUS_COMMIT_JOIN_DISCONTINUITY')
    times=[math.fsum(actual['durations'][:i]) for i in range(1,len(actual['durations'])+1)]
    matches=[i+1 for i,t in enumerate(times) if abs(t-preq['physical_handover_s'])<=1e-10]
    if len(matches)!=1 or actual['states'][matches[0]]!=p['actual_handover_state']:
        raise ValueError('PREVIOUS_HANDOVER_NOT_IN_CURRENT_ACTUAL_HISTORY')
    return previous


def verify_transaction(req,_visited=()):
    from .engine.runtime import common
    from .engine import contract
    from phase4t.execution_v2_20261020.journal_v3 import recover_v3,verify_prefix_against_actual
    def fail(condition,label):
        if not condition:raise ValueError(label)
    fail(req.get('version')=='G61_TWO_GROUP_TX_V1' and req.get('arm')=='R18_MIDFLIGHT_EVENT','TX_SCHEMA_OR_ARM')
    n=req['transaction_index'];a=req['attempt_index'];g=req['group_index']
    fail(type(n) is int and 1<=n<=4 and n not in _visited and len(_visited)<4,'TRANSACTION_INDEX_OR_ANCESTRY_CYCLE')
    fail(type(a) is int and 1<=a<=MAX_ATTEMPTS_PER_GROUP and type(g) is int and 1<=g<=MAX_GROUPS,'LOCAL_ATTEMPT_OR_GROUP')
    fail(req['reference_index']==g-1,'UNREGISTERED_REFERENCE_OR_GROUP')
    visited=(*_visited,n)
    rootpath=Path(req['root_request_path']).resolve();base=rootpath.parent
    fail(rootpath.is_relative_to((common.D/'runs').resolve()) and rootpath.name=='request.json','ROOT_REQUEST_PATH')
    root=read(rootpath);contract.verify_root(root)
    fail(req['root_request_sha256']==content(root),'TX_ROOT_BINDING')
    fail(req['runtime_freeze_sha256']==root['runtime_freeze_sha256'] and req['epoch']==root['epoch'],'TX_SOURCE_BINDING')
    tx=base/'transactions'/f'{n:02d}';anchorpath=tx/'anchor.json'
    fail(req['anchor_file_sha256']==sha(anchorpath),'ACTUAL_ANCHOR_FILE')
    anchor=common.unpack(root,'ACTUAL_ANCHOR',read(anchorpath))
    fail(req['nonce']==content([root['nonce'],n,req['anchor_file_sha256']]),'TX_NONCE')
    fail(req['run_id']==root['run_id']+f'.tx{n:02d}','TX_RUN_ID')
    prefixpath=contract.within(base,anchor['actual_prefix_file'])
    fail(sha(prefixpath)==anchor['actual_prefix_file_sha256'],'ACTUAL_PREFIX_FILE')
    actual=read(prefixpath)['trajectory']
    fail(actual['state_start']==root['case']['route']['state_start'] and actual['state_goal']==root['case']['route']['state_goal'],'ACTUAL_PREFIX_GLOBAL_BOUNDARIES')
    fail(anchor['physical_t']==math.fsum(actual['durations']) and anchor['actual_state']==actual['states'][-1],'ACTUAL_ANCHOR_STATE_OR_TIME')
    recovered=recover_v3(contract.within(base,anchor['journal_snapshot_dir']),contract.binding(root));verify_prefix_against_actual(recovered,actual,require_complete=True)
    fail(recovered['in_flight_tail']['kind']=='NO_TAIL' and recovered['prefix_head_sha256']==anchor['durable_journal_head'],'ACTUAL_ANCHOR_DURABILITY')
    refpath=contract.within(base,anchor['reference_file']);ref=read(refpath)
    fail(sha(refpath)==anchor['reference_file_sha256'] and ref['reference_index']==g-1 and refpath==(base/'references'/f'{g-1:02d}.json').resolve(),'CURRENT_REFERENCE_IDENTITY')
    previous=previous_reference(req,root,base,ref,actual,anchor,visited)
    fail(n==previous+a,'GLOBAL_TRANSACTION_NUMBERING')
    offset=anchor['physical_t']-ref['origin_physical_s'];fail(offset>=0,'REFERENCE_CLOCK_BEFORE_ORIGIN')
    expected=contract.carrier(ref['trajectory'],offset,anchor['actual_state']);case=req['case']
    from .roles import validate_input
    validate_input(case)
    for key in ['mission_role','required_committed_groups','causal_prepare_policy']:
        fail(content(case.get(key))==content(root['case'].get(key)),'TRANSACTION_ROLE_INHERITANCE_CHANGED:'+key)
    fail(content(case['route'])==content(expected),'ACTUAL_START_CARRIER_OR_REFERENCE_TAIL_CHANGED')
    for key in ['provider','rectangles','radius_m','z_layer_msl_m']:
        fail(content(case[key])==content(root['case'][key]),'GLOBAL_CONTEXT_CHANGED:'+key)
    fail(content(case['immutable_original_route'])==content(root['case']['route']),'INITIAL_PLANNED_ROUTE_CHANGED')
    fail(case['route']['state_goal']==root['case']['route']['state_goal'],'ORIGINAL_GOAL_OR_RAW_LIFT_CHANGED')
    fail(case['prefix_policy']==prefix_controls(expected,req['prefix_duration_s']),'REMAINING_PREFIX_CHANGED')
    fail('faults' not in case and 'future_faults' not in case,'SOLVER_PRIVATE_FUTURE_SCHEDULE')
    verify_clock(req,root,anchor);origin,w=group(req,root,base,common)
    if a==1:
        fail(anchor['physical_t']==origin['physical_t'] and anchor['actual_state']==origin['actual_state'],'FIRST_TRIGGER_STATE_CHANGED')
        heading=contract.nominal_heading(ref['trajectory'],offset)
        from .engine.switch import authorized_anchor
        fail(authorized_anchor(root,anchor,ref,g,heading),'UNTRIGGERED_FIRST_ATTEMPT')
        fail(req.get('prior_attempt_index') is None,'FIRST_ATTEMPT_HAS_PRIOR')
    else:
        fail(req['prior_attempt_index']==n-1,'RESTART_ANCESTRY')
        prior=base/'transactions'/f'{n-1:02d}';priorreq=read(prior/'request.json')
        verify_transaction(priorreq,_visited=visited)
        invalid=common.unpack(root,'ATTEMPT_INVALIDATION',read(prior/'invalidation.json'))
        fail(priorreq['group_index']==g and priorreq['attempt_index']==1 and priorreq['previous_committed_transaction_index']==req['previous_committed_transaction_index'],'RESTART_CROSSED_GROUP_OR_REFERENCE')
        fail(invalid['attempt_request_sha256']==content(priorreq) and invalid['attempt_index']==1 and invalid['transaction_index']==n-1 and invalid['group_index']==g and invalid['stale_workers_reaped'] is True,'PRIOR_ATTEMPT_NOT_QUARANTINED')
        fail(priorreq['group_origin_sha256']==req['group_origin_sha256'] and priorreq['physical_handover_s']==req['physical_handover_s'],'RESTART_GROUP_CHANGED')
        first_anchor=common.unpack(root,'ACTUAL_ANCHOR',read(prior/'anchor.json'))
        fail(first_anchor['physical_t']==origin['physical_t'] and first_anchor['actual_state']==origin['actual_state'],'FIRST_ATTEMPT_ORIGIN_CHANGED')
        fail(actual['states'][first_anchor['row_index']]==origin['actual_state'],'ORIGINAL_TRIGGER_NOT_IN_ACTUAL_HISTORY')
        fail(invalid['reason']=='OBSERVED_PREPARATION_CONTROL_MISMATCH','UNREGISTERED_RESTART_REASON')
        rowindex=invalid['row_index'];rows=recovered['rows']
        fail(type(rowindex) is int and 1<=rowindex<len(rows),'INVALIDATION_NOT_BEFORE_RESTART')
        row=rows[rowindex-1]
        fail(row['state']==invalid['actual_state'] and row['t']==invalid['physical_t'],'INVALIDATION_ACTUAL_STATE')
        fail(row['u']==invalid['actual_control'] and row['commanded_u']==invalid['requested_control'] and row['u']!=row['commanded_u'],'INVALIDATION_NOT_OBSERVED_MISMATCH')
        fail(origin['physical_t']<row['t']<w.physical_handover,'INVALIDATION_OUTSIDE_ORIGINAL_PREFIX')
        import numpy as np
        from .engine.boundary_clock import active_index,absolute_ends
        schedule=absolute_ends(ref['origin_physical_s'],np.cumsum(ref['trajectory']['durations']))
        for j in range(first_anchor['row_index'],len(rows)):
            start=rows[j-1]['t'] if j else 0.
            requested=ref['trajectory']['controls'][active_index(schedule,start)]
            fail(rows[j]['commanded_u']==requested,'PREPARATION_COMMAND_NOT_COMMITTED_SCHEDULE')
        normal=next((r for r in rows[rowindex:] if r['u']==r['commanded_u']),None)
        fail(normal is not None and normal['index']==anchor['row_index'] and normal['state']==anchor['actual_state'],'RESTART_NOT_FIRST_NORMAL_OBSERVATION')
        fail(invalid['observed_perf']<=anchor['observation_perf']<w.first_deadline,'RESTART_OBSERVATION_CLOCK')
    return True
