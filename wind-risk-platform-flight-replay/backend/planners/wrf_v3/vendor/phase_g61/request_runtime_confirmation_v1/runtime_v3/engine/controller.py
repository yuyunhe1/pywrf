"""Root-owned continuous plant, event triggers, and bounded fresh replans."""
import copy,math,os,subprocess,sys,time,traceback
from pathlib import Path
from types import SimpleNamespace
from functools import partial
from unittest.mock import patch
import numpy as np
from . import contract
from phase_g61.ordered_acceptance_cost_v1 import publication_barrier
from ..phase_stamp import persist_phase
from ..stream_core import PrefixStream,StreamRejected
from ..stream_receipt import publish as publish_stream_prefix
from ..plant import Plant
from .. import parent_handoff,reference_preview
from .runtime.common import D as RUNTIME
from ..prefix_receipt import validate as validate_prefix_receipt
from phase_g61.common_research_entry_v1 import local_native as recovery
from phase4t.repair_v1.progress import OrderedProgress
from phase4t.capture_g4_20261020.controller import advance as feedback_advance
from .switch import trigger_kind
from .boundary_clock import absolute_ends,active_index
from .runtime.common import read,put,sha,content,packet,unpack,verify,source_check,Clock,Stop,Limit,control_ok,ROOT,result_schema
from .runtime.environment import Context,load
from .runtime.commit_contract import publish_commit
from .runtime.publication import validate_publication
from phase4o.ownership import OwnedJob
from phase4t.amendment_v1.model import PlannedRoute,Reference,DomainFail
from phase3b.integrator import SpatialIntegrationError
from phase4t.execution_v2_20261020.journal_v3 import JournalV3,recover_v3,verify_prefix_against_actual
from phase4t_g61.wrf_feasibility_validation_v1.runtime.accuracy import integrate
from phase4t_g61.wrf_feasibility_validation_v1.runtime.contract import join_ok,lift_ok,PT,HT
from phase_g61.uncovered_environments_v1.held_control import propagate

class OrdinaryFailure(Exception):pass

def binary(path,data):
    with path.open('xb') as stream:
        if stream.write(data)!=len(data):raise Stop('SNAPSHOT_SHORT_WRITE')
        stream.flush();os.fsync(stream.fileno())
    if path.read_bytes()!=data:raise Stop('SNAPSHOT_READBACK')

def prefix_dynamics(c,trajectory):
    from phase3b.validator import THRESHOLDS
    if c['thresholds']!=THRESHOLDS or c['domain_status']!='IN_DOMAIN_AT_EVALUATED_POINTS':raise Stop('GLOBAL_PREFIX_C_DOMAIN_OR_THRESHOLD')
    if any(f not in ('POSITION_RESIDUAL','HEADING_RESIDUAL') for f in c['failures']):raise Stop('GLOBAL_PREFIX_C_MISMATCH:'+str(c['failures']))
    a=np.asarray(trajectory['states']);b=np.asarray([r[1:] for r in c['diagnostic_nodes']])
    if a.shape!=b.shape or not np.isfinite(a).all() or not np.isfinite(b).all() or np.max(np.linalg.norm(a[:,:2]-b[:,:2],axis=1))>PT or np.max(np.abs(a[:,2]-b[:,2]))>HT:
        raise Stop('GLOBAL_PREFIX_ACTUAL_NODE_MISMATCH')

def main(request_path,base_path):
    root=read(request_path);base=Path(base_path);clock=Clock(root['owner_deadline']);case=root['case'];z=case['z_layer_msl_m']
    journal=None;actual=None;transactions=[];attempts=[];trace=[];faults=[];plant_stats=dict(held_ODE_solves=0,RHS_evaluations=0,checkpoint_rows=0)
    phase='ROOT_SOURCE_CHECK';stages={};status='REPLAN_NO_VALID_ROUTE';fatal=False;strict=False;last=None
    try:
        contract.verify_root(root);source_check();clock.check()
        t=time.perf_counter();raw=load(case);ctx=Context(case,raw,clock);stages['root_context_s']=time.perf_counter()-t
        source=case['initial_plan_source']
        if sha(ROOT/source['route_file'])!=source['route_file_sha256'] or sha(ROOT/source['parent_receipt_file'])!=source['parent_receipt_sha256']:
            raise Stop('INITIAL_PLANNED_ROUTE_SOURCE_CHANGED')
        receipt=read(ROOT/source['parent_receipt_file'])
        if not receipt['C']['valid'] or not receipt['collision']['valid'] or receipt['trajectory_sha256']!=content(case['route']):raise Stop('INITIAL_PLANNED_ROUTE_RECEIPT')
        initial=PlannedRoute.from_trajectory(case['route']);initial.verify()
        phase='INITIAL_MONITOR_REFERENCE';t=time.perf_counter()
        with ctx.evaluation():reference=Reference(initial,ctx.provider,clock)
        stages['initial_monitor_reference_s']=time.perf_counter()-t
        reference_origin=0.;reference_index=0;transaction_counter=0
        refdoc=dict(trajectory=case['route'],origin_physical_s=0.,reference_index=0,initial_plan_source=source)
        refpath=base/'references/00.json';put(refpath,refdoc)
        q=np.array(case['route']['state_start'],float);actual=copy.deepcopy(case['route']);actual.update(controls=[],durations=[],states=[q.tolist()])
        journal=JournalV3(base/'journal',contract.binding(root))
        local_progress=OrderedProgress(reference,q);local_field=recovery.Field(ctx.provider,z,case['local_recovery_policy']['native_interval_local_s']);feedback_trace=[]
        private_path=RUNTIME.parent/'PLANT_INPUT.json'
        if sha(private_path)!=case['private_plant_sha256']:raise Stop('PRIVATE_PLANT_INPUT_IDENTITY')
        plant=Plant(read(private_path));last_telemetry=None
        phase='INDEPENDENT_PREFIX_STREAM_SETUP';stream_begin=time.perf_counter()
        cstreamctx=Context(case,load(case),clock);costreamctx=Context(case,load(case),clock)
        prefix_stream=PrefixStream(case['route'],cstreamctx,costreamctx)
        stages['independent_prefix_stream_setup_s']=time.perf_counter()-stream_begin
        stream_trigger_stats=None

        def physical():return math.fsum(actual['durations'])
        def observe():
            s=max(0.,min(float(reference.ends[-1]),physical()-reference_origin))
            i=min(int(np.searchsorted(reference.ends,s,side='left')),len(reference.ends)-1)
            nominal=reference.q(i,s);dp=float(np.linalg.norm(q[:2]-nominal[:2]));dh=abs(float(q[2]-nominal[2]))
            row=dict(physical_t=physical(),reference_index=reference_index,reference_parameter_s=s,
                position_error_m=dp,raw_heading_error_rad=dh,actual_state=q.tolist(),reference_state=nominal.tolist())
            trace.append(row)
            kind=trigger_kind(case,physical(),reference_index,dp,dh)
            if kind is not None:return dict(row,trigger_kind=kind,trigger_perf=time.perf_counter())
            return None

        def execute(u,d,mode,monitor,local_events=None,prepare_clock=None):
            nonlocal q,last_telemetry
            clock.check();control_ok(u,d);start=physical()
            if prepare_clock is not None:prepare_clock.check()
            actual_u,limit,marker=plant.control(start,float(u),min(.02,float(d)))
            if limit<=0:raise Stop('PLANT_ZERO_DURATION')
            if start+limit>case['physical_deadline_s']+1e-12:raise OrdinaryFailure('EXECUTION_PHYSICAL_TIMEOUT')
            fired=[]
            if local_events is not None and actual_u==float(u) and marker is None:
                with ctx.evaluation():
                    qn,used,fired=feedback_advance(q,float(actual_u),limit,ctx.provider,z,start,local_events)
                    recovery.after_event(reference,local_field,local_progress,qn,start+used,fired)
                plant_stats['feedback_ODE_solves']=plant_stats.get('feedback_ODE_solves',0)+1
                plant_stats['feedback_RHS_evaluations']='UNKNOWN_INHERITED_ADVANCE_API_DOES_NOT_RETURN_NFEV'
                plant_stats['RHS_evaluations_coverage']='NON_FEEDBACK_HELD_COMMANDS_ONLY_FEEDBACK_RHS_UNKNOWN'
            else:
                with ctx.evaluation():sol=propagate(q,float(actual_u),limit,ctx.provider,z,start)
                qn=sol.y[:,-1];used=limit;plant_stats['RHS_evaluations']+=int(sol.nfev)
            if prepare_clock is not None:prepare_clock.check()
            control_ok(float(actual_u),float(used))
            actual['controls'].append(float(actual_u));actual['durations'].append(float(used))
            if physical()<=start:raise Stop('ACTUAL_CHECKPOINT_NO_TIME_PROGRESS')
            q=np.asarray(qn);actual['states'].append(q.tolist());plant_stats['held_ODE_solves']+=1
            observed_perf=time.perf_counter()
            telemetry=plant.observe(start,float(used),float(u),float(actual_u),marker)
            last_telemetry=dict(telemetry,physical_t=physical(),actual_state=q.tolist(),row_index=len(actual['controls']),observation_perf=observed_perf)
            event=observe() if monitor else None
            journal(dict(index=len(actual['controls']),u=float(actual_u),commanded_u=float(u),d=float(used),state=q.tolist(),t=physical(),
                mode='REGISTERED_BOUNDED_ACTUATOR_PERTURBATION' if marker is not None else mode,feedback_events=fired))
            try:prefix_stream.add(float(actual_u),float(used),q.tolist())
            except StreamRejected as exc:
                value=exc.value;detail=str(value)
                if any(k in detail for k in ('DOMAIN','ROI','MISSING_DATA','INVALID_INPUT')):raise OrdinaryFailure('EXECUTION_DOMAIN_FAIL') from exc
                if exc.role=='COLLISION':raise OrdinaryFailure('EXECUTION_COLLISION') from exc
                raise Stop('INDEPENDENT_STREAM_C_FAILURE:'+detail) from exc
            if prepare_clock is not None:prepare_clock.check()
            plant_stats['checkpoint_rows']+=1
            return event


        def active_control():
            ends=absolute_ends(reference_origin,reference.ends);i=active_index(ends,physical())
            return float(reference.tr['controls'][i]),ends[i]

        def replan(event):
            nonlocal reference,reference_origin,reference_index,refpath,last,phase,stream_trigger_stats,transaction_counter
            phase='REPLAN_GROUP_CONSTRUCT'
            group_index=len(transactions)+1
            if group_index>2:raise OrdinaryFailure('PREPARATION_GROUP_LIMIT_EXHAUSTED')
            previous_committed=transactions[-1]['transaction_index'] if transactions else None
            originpath=base/'groups'/f'{group_index:02d}'/'origin.json'
            if originpath.exists():raise Stop('DUPLICATE_PREPARATION_GROUP')
            from ..window import Window,WindowExhausted
            try:window=Window.begin(physical(),event['trigger_perf'],root['owner_deadline'],case['physical_deadline_s'])
            except WindowExhausted as exc:raise OrdinaryFailure(str(exc)) from exc
            stream_trigger_stats=prefix_stream.statistics()
            origin=dict(event,group_index=group_index,physical_t=physical(),actual_state=q.tolist(),physical_handover_s=window.physical_handover,
                prepare_deadline=window.first_deadline,reference_index=reference_index)
            put(originpath,packet(root,'PREPARE_ORIGIN',origin))
            current=dict(event,observation_perf=event['trigger_perf'])
            for local_attempt in (1,2):
                transaction_counter+=1;n=transaction_counter
                try:spec=window.at(physical(),current['observation_perf'])
                except WindowExhausted as exc:raise OrdinaryFailure(str(exc)) from exc
                tx=base/'transactions'/f'{n:02d}';tx.mkdir(parents=True,exist_ok=False)
                prepare=Clock(spec['prepare_deadline']);job=process=None;prefix_jobs=[];txstages={}
                anchor_t=physical();anchor_state=q.tolist();anchor_index=len(actual['controls']);outcome='STARTED'
                try:
                    prepare.check();prefix_actual=copy.deepcopy(actual);put(tx/'actual_prefix.json',dict(trajectory=prefix_actual))
                    snap=tx/'anchor_snapshot';snap.mkdir();binary(snap/'prefix_identity.json',(base/'journal/prefix_identity.json').read_bytes());binary(snap/'prefix.jsonl',(base/'journal/prefix.jsonl').read_bytes())
                    anchor=dict(current,actual_state=anchor_state,physical_t=anchor_t,row_index=anchor_index,trigger_perf=window.first_trigger_perf,
                        actual_prefix_file=(tx/'actual_prefix.json').relative_to(base).as_posix(),actual_prefix_file_sha256=sha(tx/'actual_prefix.json'),
                        journal_snapshot_dir=snap.relative_to(base).as_posix(),durable_journal_head=journal.head,
                        reference_file=refpath.relative_to(base).as_posix(),reference_file_sha256=sha(refpath))
                    put(tx/'anchor.json',packet(root,'ACTUAL_ANCHOR',anchor))
                    carrier=contract.carrier(reference.tr,anchor_t-reference_origin,anchor_state)
                    tc=copy.deepcopy(case);tc.update(id=case['id']+f'.ATTEMPT{n:02d}',route=carrier,
                        immutable_original_route=case['route'],global_physical_offset_s=anchor_t,
                        physical_deadline_s=case['physical_deadline_s']-anchor_t,prefix_policy=contract.prefix_controls(carrier,spec['prefix_duration_s']),
                        execution_descriptor_not_original_planned_route=True)
                    phase='REPLAN_PARENT_CONTEXT';pctx=Context(tc,raw,prepare)
                    req=dict(version='G61_TWO_GROUP_TX_V1',run_id=root['run_id']+f'.tx{n:02d}',arm='R18_MIDFLIGHT_EVENT',case=tc,
                        transaction_index=n,attempt_index=local_attempt,group_index=group_index,reference_index=reference_index,previous_committed_transaction_index=previous_committed,
                        prior_attempt_index=None if local_attempt==1 else n-1,group_origin_file=originpath.relative_to(base).as_posix(),group_origin_sha256=sha(originpath),
                        root_request_path=str(Path(request_path).resolve()),root_request_sha256=content(root),anchor_file_sha256=sha(tx/'anchor.json'),
                        epoch=root['epoch'],runtime_freeze_sha256=root['runtime_freeze_sha256'],owner_start=window.first_trigger_perf,
                        owner_deadline=root['owner_deadline'],prepare_deadline=prepare.deadline,prefix_duration_s=spec['prefix_duration_s'],
                        physical_handover_s=window.physical_handover,attempt_observation_perf=current['observation_perf'],
                        parent_context_id=pctx.id,suffix_context_id=pctx.at_offset(spec['prefix_duration_s']).id)
                    req['nonce']=content([root['nonce'],n,req['anchor_file_sha256']]);put(tx/'request.json',req)
                    phase='REPLAN_PREFIX_VALIDATORS_SPAWN';source_check();prepare.check();prefix_begin=time.perf_counter()
                    # No cold prefix workers: the independent original row work is already current.
                    verify(req)
                    phase='REPLAN_SOLVER_SPAWN';spawn=time.perf_counter()
                    with (tx/'solver_stdout.txt').open('x') as out,(tx/'solver_stderr.txt').open('x') as err:
                        process=subprocess.Popen([sys.executable,'-X','utf8','-B','-m','phase_g61.request_runtime_confirmation_v1.runtime_v3.engine.runtime.solver_worker',str(tx/'request.json'),str(tx)],
                            cwd=ROOT,stdout=out,stderr=err,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    job=OwnedJob(process);txstages['solver_spawn_s']=time.perf_counter()-spawn
                    phase='REPLAN_CAUSAL_PREDICTION';us=tc['prefix_policy']['controls'];ds=tc['prefix_policy']['durations'];t=time.perf_counter()
                    with pctx.evaluation():predicted,pnodes=integrate(anchor_state,us,ds,pctx.provider,z)
                    txstages['prediction_s']=time.perf_counter()-t
                    put(tx/'solver_input.json',packet(req,'SOLVER_INPUT',dict(predicted_handover_state=predicted.tolist(),prefix_nodes=pnodes,
                        physical_handover_s=window.physical_handover,local_handover_s=spec['prefix_duration_s'],
                        prediction_source='OBSERVED_ACTUAL_ANCHOR_PLUS_COMMITTED_NOMINAL_CONTROLS_AND_KNOWN_WRF_ONLY',
                        anchor_observation_perf=current['observation_perf'],prediction_published_perf=time.perf_counter())))
                    phase='REPLAN_DURABLE_PREFIX_STREAM_PUBLICATION'
                    publish_stream_prefix(req,root,prefix_stream,prefix_actual,ctx.id,tx,prepare,stream_trigger_stats)
                    phase='REPLAN_ACTUAL_MOVING_PREFIX';t=time.perf_counter();invalid=None
                    while physical()<window.physical_handover-1e-12:
                        u,end=active_control();duration=min(.02,end-physical(),window.physical_handover-physical())
                        if duration<=0:raise Stop('CAUSAL_PREFIX_NOMINAL_BOUNDARY')
                        execute(u,duration,'ACTIVE_REPLAN_PREPARATION_PREFIX',False,prepare_clock=prepare)
                        if last_telemetry['actuator_mismatch']:invalid=dict(last_telemetry);break
                    txstages['actual_prefix_until_event_s']=time.perf_counter()-t
                    if invalid is not None:
                        # Reap before publishing the immutable quarantine record.
                        if job is not None:job.close();job=None
                        for _,_,pjob in prefix_jobs:pjob.close()
                        inv=dict(reason='OBSERVED_PREPARATION_CONTROL_MISMATCH',attempt_index=local_attempt,transaction_index=n,group_index=group_index,attempt_request_sha256=content(req),
                            row_index=invalid['row_index'],physical_t=invalid['physical_t'],actual_state=invalid['actual_state'],
                            actual_control=invalid['actual_control'],requested_control=invalid['requested_control'],
                            observed_perf=invalid['observation_perf'],stale_workers_reaped=True,
                            original_physical_handover_s=window.physical_handover,original_wall_deadline=window.first_deadline,
                            candidate_publication_present=(tx/'solver_publication.json').exists(),strict_task_success=False)
                        put(tx/'invalidation.json',packet(root,'ATTEMPT_INVALIDATION',inv));outcome='CANCELLED_OBSERVED_MISMATCH'
                        attempts.append(dict(transaction_index=n,attempt_index=local_attempt,group_index=group_index,status=outcome,observation=inv,request_sha256=content(req)))
                        if local_attempt>=2:raise OrdinaryFailure('CAUSAL_ATTEMPT_LIMIT_EXHAUSTED')
                        phase='WAIT_FIRST_OBSERVED_NORMAL_ACTUATOR';groupclock=Clock(window.first_deadline)
                        while physical()<window.physical_handover-1e-12:
                            u,end=active_control();execute(u,min(.02,end-physical(),window.physical_handover-physical()),'CAUSAL_WAIT_OBSERVE_ACTUATOR',False,prepare_clock=groupclock)
                            if not last_telemetry['actuator_mismatch']:break
                        if last_telemetry['actuator_mismatch']:raise OrdinaryFailure('NO_NORMAL_OBSERVATION_BEFORE_ORIGINAL_HANDOVER')
                        current=dict(trigger_kind='CAUSAL_PREPARATION_RESTART',observation_perf=last_telemetry['observation_perf'],trigger_perf=window.first_trigger_perf)
                        continue
                    if abs(physical()-window.physical_handover)>1e-10:raise Stop('PHYSICAL_HANDOVER_TIME_CHANGED')
                    ok,join=join_ok(q.tolist(),predicted.tolist())
                    if not ok:raise Stop('REPLAN_JOIN_DISCONTINUITY_WITHOUT_OBSERVED_CONTROL_CHANGE')
                    phase='REPLAN_COLLECT_FULL_PREFIX_VALIDATORS';values={}
                    for role in ('C','COLLISION'):
                        prepare.check()
                        path=tx/('prefix_'+role+'_result.json');value=unpack(req,'PREFIX_'+role,read(path))
                        publication=unpack(req,'PREFIX_'+role+'_PUBLICATION',read(tx/('prefix_'+role+'_publication.json')))
                        validate_prefix_receipt(value,publication,req,role,sha(path),time.perf_counter(),prepare.deadline,Stop,Limit)
                        if value['status']=='LIMIT':raise Limit('PREFIX_'+role+'_BUDGET')
                        if value['status']=='DOMAIN_REJECT':raise OrdinaryFailure('EXECUTION_DOMAIN_FAIL')
                        if value['status']=='COLLISION_REJECT':raise OrdinaryFailure('EXECUTION_COLLISION')
                        if value['status']!='PASS':raise Stop('PREFIX_'+role+'_STRUCTURAL:'+value.get('detail',''))
                        if value['request_sha256']!=content(req) or value['actual_prefix_sha256']!=content(prefix_actual) or value['root_context_id']!=ctx.id:raise Stop('PREFIX_VALIDATOR_CONTEXT_OR_INPUT')
                        values[role]=value
                    pc=values['C']['value'];pco=values['COLLISION']['value'];prefix_dynamics(pc,prefix_actual)
                    put(tx/'prefix_assessment.json',dict(C_original_goal=pc,collision=pco,prefix_dynamics_pass=True,not_task_success=True,
                        actual_prefix_sha256=content(prefix_actual),root_context_id=ctx.id,validator_receipts={role:sha(tx/('prefix_'+role+'_result.json')) for role in values}))
                    txstages['parallel_prefix_validation_inclusive_s']=time.perf_counter()-prefix_begin
                    phase='REPLAN_WAIT_SOLVER_PUBLICATION'
                    preview=None;preview_attempted=False
                    while not publication_barrier.complete(tx) and process.poll() is None:
                        prepare.check()
                        if not preview_attempted and (tx/'unvalidated_suffix_proposal.json').exists():
                            preview_attempted=True;pt=time.perf_counter()
                            preview=reference_preview.build(req,pctx,carrier,us,ds,pnodes,tx)
                            txstages['speculative_reference_s']=time.perf_counter()-pt
                        time.sleep(.002)
                    publication_barrier.check_after_wait(tx,process,prepare,Stop)
                    selected=unpack(req,'SOLVER_RESULT',read(tx/'solver_result.json'))
                    result_schema(selected,'SOLVER_RESULT')
                    if selected['status']=='STRUCTURAL_STOP':raise Stop('SOLVER_STRUCTURAL_STOP:'+selected.get('detail',''))
                    pub=unpack(req,'SOLVER_PUBLICATION',read(tx/'solver_publication.json'));validate_publication(pub,req,sha(tx/'solver_result.json'));prepare.check()
                    if not pub['timely'] or selected['result'] is None:raise OrdinaryFailure(selected['status'])
                    suffix=selected['result']['trajectory']
                    if suffix['state_start']!=predicted.tolist() or suffix['state_goal']!=case['route']['state_goal']:raise Stop('SOLVER_ACTUAL_GOAL_IDENTITY')
                    whole=copy.deepcopy(carrier);whole.update(controls=us+suffix['controls'],durations=ds+suffix['durations'],states=pnodes+suffix['states'][1:])
                    total=math.fsum(whole['durations'])
                    if total>tc['physical_deadline_s']:raise OrdinaryFailure('REPLAN_PHYSICAL_HORIZON_EXHAUSTED')
                    phase='REPLAN_PARENT_FULL_C_COLLISION';t=time.perf_counter()
                    parent_handoff.submit(req,whole,pctx,tx)
                    ct=time.perf_counter();co=pctx.checker.check(whole);txstages['parent_collision_s']=time.perf_counter()-ct
                    put(tx/'parent_collision_body.json',packet(req,'PARENT_COLLISION_RESULT',dict(value=co,trajectory_sha256=content(whole),context_id=pctx.id)))
                    if not co['valid']:raise OrdinaryFailure('REPLAN_COLLISION_REJECT')
                    while not (tx/'parent_C_publication.json').exists() and process.poll() is None:prepare.check();time.sleep(.002)
                    c,cv=parent_handoff.consume(req,whole,pctx,tx,prepare)
                    txstages['parent_C_worker_s']=cv['C_s']
                    process.wait(timeout=max(.001,prepare.deadline-time.perf_counter()));job.close();job=None
                    if process.returncode!=0:raise Stop('PARENT_C_SOLVER_NONZERO_EXIT')
                    if not c['valid'] or not lift_ok(c['diagnostic_final_state'][2],whole['state_goal'][2]):raise Stop('PARENT_FULL_C_OR_LIFT_MISMATCH')
                    if not co['valid']:raise OrdinaryFailure('REPLAN_COLLISION_REJECT')
                    put(tx/'parent_validation.json',dict(C=c,collision=co,trajectory_sha256=content(whole),request_sha256=content(req),context_id=pctx.id))
                    txstages['parent_C_collision_s']=time.perf_counter()-t
                    phase='REPLAN_REQUIRED_MONITOR_REFERENCE';t=time.perf_counter()
                    if preview is not None and preview[0]==content(whole):
                        newref=preview[1];txstages['reference_preview_exact_match']=True
                    else:
                        with pctx.evaluation():newref=Reference(PlannedRoute.from_trajectory(whole),pctx.provider,prepare)
                        txstages['reference_preview_exact_match']=False
                    txstages['required_monitor_reference_s']=time.perf_counter()-t
                    prepared=packet(req,'PREPARED_ROUTE',dict(trajectory=whole,trajectory_sha256=content(whole),full_validation_sha256=sha(tx/'parent_validation.json'),
                        prefix_assessment_sha256=sha(tx/'prefix_assessment.json'),predicted_handover_state=predicted.tolist(),actual_handover_state=q.tolist(),
                        join=join,context_id=pctx.id,published_perf=time.perf_counter(),solver_reaped=True))
                    put(tx/'prepared.json',prepared);phase='REPLAN_EXTERNAL_ACK'
                    while not (tx/'prepared_ack.json').exists():prepare.check();time.sleep(.002)
                    ack=unpack(req,'PREPARED_ACK',read(tx/'prepared_ack.json'))
                    if not ack['accepted'] or ack['prepared_sha256']!=prepared['sha256']:raise OrdinaryFailure('PREPARED_ACK_LATE_OR_REFUSED')
                    phase='REPLAN_DURABLE_COMMIT';publication=publish_commit(req,tx,prepared['sha256'],q.tolist(),join,prepare)
                    if not publication['accepted']:raise OrdinaryFailure('REPLAN_BUDGET_EXCEEDED')
                    row=dict(publication,transaction_index=n,group_index=group_index,attempt_index=local_attempt,trigger_physical_s=anchor_t,
                        first_trigger_physical_s=window.physical_origin,actual_handover_physical_s=physical(),anchor_row=anchor_index,
                        trajectory_sha256=content(whole),goal_unchanged=True,stages=txstages,solver_result_file_sha256=sha(tx/'solver_result.json'),
                        solver_reaped=True,join=join,remaining_global_physical_s=case['physical_deadline_s']-physical(),
                        prepare_inclusive_s=publication['persisted_readback_perf']-window.first_trigger_perf,
                        attempt_inclusive_s=publication['persisted_readback_perf']-current['observation_perf'],prefix_duration_s=spec['prefix_duration_s'])
                    put(tx/'admission.json',packet(req,'TX_ADMISSION',row));transactions.append(row);outcome='COMMITTED'
                    attempts.append(dict(transaction_index=n,attempt_index=local_attempt,group_index=group_index,status=outcome,request_sha256=content(req)))
                    reference=newref;reference_origin=anchor_t;reference_index=group_index
                    refpath=base/'references'/f'{reference_index:02d}.json';put(refpath,dict(trajectory=whole,origin_physical_s=anchor_t,reference_index=reference_index,committed_transaction_index=n,source_prepared_sha256=prepared['sha256']))
                    last=dict(req=req,anchor_row=anchor_index,trajectory=whole,case=tc)
                    return
                finally:
                    if job is not None:job.close()
                    for _,_,pjob in prefix_jobs:pjob.close()
                    put(tx/'stage_costs.json',dict(last_phase=phase,outcome=outcome,stages=txstages,observed_perf=time.perf_counter(),
                        prepare_deadline=prepare.deadline,original_prepare_deadline=window.first_deadline,first_trigger_perf=window.first_trigger_perf,
                        inclusive_since_first_trigger_s=time.perf_counter()-window.first_trigger_perf,prefix_duration_s=spec['prefix_duration_s'],
                        original_physical_handover_s=window.physical_handover,posthoc_cannot_promote_late_result=True))


        phase='ACTUAL_FLIGHT_MONITOR_AND_REPLAN'
        while True:
            clock.check();now=physical();finish=absolute_ends(reference_origin,reference.ends)[-1]
            if now>=finish:break
            local_events=None
            if reference_index==0:
                with ctx.evaluation():
                    local_progress.observe(q,now)
                    if not local_progress.complete:local_progress.boundary(q,now)
                    u,local_diag,local_events=recovery.command(reference,local_field,local_progress,q,actual_t=now)
                    endpoint=local_progress.endpoint_event()
                    if endpoint is not None:local_events=[*local_events,endpoint]
                feedback_trace.append(dict(physical_t=now,reference_s=local_progress.s,segment=local_progress.i,diagnostics=local_diag))
                if u is None:
                    event=observe()
                    if event is None:raise OrdinaryFailure('LOCAL_RECOVERY_UNSUPPORTED_WITHOUT_AUTHORIZED_TRIGGER')
                    replan(event);continue
                switch=now+.02
            else:u,switch=active_control()
            event=execute(u,min(switch,finish)-now,'LOCAL_TIME_AWARE_RECOVERY' if reference_index==0 else 'TRACK_CURRENT_COMMITTED_ZOH',True,local_events=local_events)
            if event is not None:replan(event)
        journal.close();journal=None
        put(base/'executed.json',dict(trajectory=actual,binding=contract.binding(root),physical_time_s=physical(),trajectory_sha256=content(actual)))
        phase='GLOBAL_WHOLE_ACTUAL_C';t=time.perf_counter();c=ctx.replay(actual,physical());prefix_dynamics(c,actual);stages['global_actual_C_s']=time.perf_counter()-t
        persist_phase(put,base,root,phase,t,stages['global_actual_C_s'],c)
        phase='GLOBAL_WHOLE_ACTUAL_COLLISION';t=time.perf_counter();co=ctx.checker.check(actual);stages['global_actual_collision_s']=time.perf_counter()-t
        persist_phase(put,base,root,phase,t,stages['global_actual_collision_s'],co)
        if not co['valid']:raise OrdinaryFailure('EXECUTION_COLLISION')
        if last is None:raise OrdinaryFailure('NO_MIDFLIGHT_REPLAN_WAS_EXERCISED')
        offset=last['anchor_row'];sub=copy.deepcopy(actual);sub.update(state_start=actual['states'][offset],controls=actual['controls'][offset:],durations=actual['durations'][offset:],states=actual['states'][offset:])
        lctx=Context(last['case'],raw,clock);phase='LAST_NEW_ROUTE_ACTUAL_C';t=time.perf_counter();lc=lctx.replay(sub,math.fsum(sub['durations']));stages['last_route_actual_C_s']=time.perf_counter()-t
        persist_phase(put,base,root,phase,t,stages['last_route_actual_C_s'],lc)
        from phase4t_g61.wrf_feasibility_validation_v1.runtime import ordered,boundaries
        from phase_g61.online_execution_repair_v1.compensated_dop853 import CompensatedDOP853,solve_ivp
        from phase_g61.uncovered_environments_v1.committed_events import integrate as enclosed
        from phase_g61.wind_column_diagnostic_v1.progress_geometry import progress as memo_progress
        from .runtime.replay import PartitionReplay
        from .ordered_process import assess
        lastref=SimpleNamespace(route=PlannedRoute.from_trajectory(last['trajectory']),tr=last['trajectory'],provider=lctx.provider)
        phase='LAST_NEW_ROUTE_INDEPENDENT_ORDERED';t=time.perf_counter();memo={}
        with lctx.evaluation(),patch.object(ordered,'PartitionReplay',PartitionReplay),patch.object(boundaries,'DOP853',CompensatedDOP853),patch.object(boundaries,'solve_ivp',solve_ivp),patch.object(boundaries.PartitionReplay,'integrate',enclosed),patch.object(ordered,'progress',partial(memo_progress,clock=clock,statistics=memo)):
            assessed=assess(lc,sub,lastref,last['req'],base/'final_ordered',clock,'phase_g61.request_runtime_confirmation_v1.runtime_v3.engine.runtime')
        stages['last_route_ordered_s']=time.perf_counter()-t
        persist_phase(put,base,root,phase,t,stages['last_route_ordered_s'],assessed)
        recovered=recover_v3(base/'journal',contract.binding(root));verify_prefix_against_actual(recovered,actual,require_complete=True)
        from ..roles import groups_pass
        strict=bool(groups_pass(case,transactions) and c['valid'] and co['valid'] and lc['valid'] and assessed['complete'] and lift_ok(c['diagnostic_final_state'][2],case['route']['state_goal'][2]) and physical()<=case['physical_deadline_s'] and plant.assessment(physical())['all_registered_pulses_fully_applied'])
        put(base/'assessment.json',dict(global_C=c,global_collision=co,last_new_route_C=lc,last_new_route_ordered=assessed,
            actual_whole_no_reset=True,original_path_recovery='NOT_DEMONSTRATED',last_route_origin_physical_s=last['case']['global_physical_offset_s'],
            actual_flight_s=physical(),remaining_global_physical_s=case['physical_deadline_s']-physical(),
            position_error_m=c['position_error'],raw_heading_error_rad=c['diagnostic_final_state'][2]-case['route']['state_goal'][2],
            control_saturation_s=math.fsum(d for u,d in zip(actual['controls'],actual['durations']) if abs(u)>=10/8.513422179807296-1e-12)))
        status='TASK_SUCCESS' if strict else 'EXECUTION_TERMINAL_OR_ORDERED_FAIL';clock.check()
    except (Limit,subprocess.TimeoutExpired) as exc:
        status='OFFLINE_PIPELINE_WALL_TIMEOUT' if time.perf_counter()>=root['owner_deadline'] else 'REPLAN_BUDGET_EXCEEDED';strict=False
        put(base/'timeout.json',dict(detail=str(exc),phase=phase,observed_perf=time.perf_counter(),actual_physical_t=math.fsum(actual['durations']) if actual else None))
    except OrdinaryFailure as exc:
        status=str(exc);strict=False;put(base/'ordinary_failure.json',dict(status=status,phase=phase,actual_motion_after_last_checkpoint='NOT_SIMULATED_NO_SAFE_ABORT_CLAIM'))
    except (DomainFail,SpatialIntegrationError) as exc:
        domain=str(exc) in ('CONNECTOR_OUT_OF_DOMAIN','OUT_OF_ROI','OUT_OF_HORIZONTAL_DOMAIN','BELOW_TERRAIN','BELOW_VERTICAL_DOMAIN','ABOVE_VERTICAL_DOMAIN','MISSING_DATA','OUT_OF_TIME_DOMAIN','TIME_GAP_EXCEEDS_CONTRACT')
        status='EXECUTION_DOMAIN_FAIL' if domain else 'STRUCTURAL_STOP';fatal=not domain;strict=False
        put(base/'domain_failure.json',dict(detail=str(exc),phase=phase,status=status,traceback=traceback.format_exc()))
    except BaseException as exc:
        status='STRUCTURAL_STOP';fatal=True;strict=False
        put(base/'failure.json',dict(detail=type(exc).__name__+': '+str(exc),traceback=traceback.format_exc(),phase=phase))
    finally:
        if journal is not None:journal.close()
        if actual is not None and not (base/'executed.json').exists():put(base/'partial_executed.json',dict(trajectory=actual,partial=True,physical_time_s=math.fsum(actual['durations'])))
        if actual is not None and actual['controls'] and not (base/'assessment.json').exists() and not fatal and 'ctx' in locals() and time.perf_counter()<root['owner_deadline']-5.:
            try:
                total=math.fsum(actual['durations']);failed_c=ctx.replay(actual,total);prefix_dynamics(failed_c,actual)
                failed_co=ctx.checker.check(actual)
                put(base/'failed_actual_assessment.json',dict(C_original_goal=failed_c,collision=failed_co,
                    independent_actual_prefix_dynamics_pass=True,strict_task_success=False,
                    actual_trajectory_sha256=content(actual),late_revalidation_cannot_promote_success=True))
            except Limit:
                put(base/'failed_actual_assessment_not_completed.json',dict(status='NOT_RUN_TO_COMPLETION_GLOBAL_BUDGET',strict_task_success=False))
            except BaseException as exc:
                status='STRUCTURAL_STOP';fatal=True;strict=False
                put(base/'failed_actual_assessment_error.json',dict(detail=str(exc),traceback=traceback.format_exc()))
        if 'prefix_stream' in locals():put(base/'independent_prefix_stream_stats.json',dict(final=prefix_stream.statistics(),at_first_trigger=stream_trigger_stats))
        if 'plant' in locals():
            private_assessment=plant.assessment(math.fsum(actual['durations']));faults=private_assessment['faults'];put(base/'plant_disturbance_assessment.json',private_assessment)
        put(base/'monitor_trace.json',trace);put(base/'faults.json',faults);put(base/'plant_compute_stats.json',plant_stats)
        if 'feedback_trace' in locals():put(base/'local_feedback_trace.json',dict(rows=feedback_trace,predictor=getattr(local_progress,'temporal_stats',{}),local_field_queries=local_field.queries))
        if 'raw' in locals():put(base/'owner_query_stats.json',raw.query_stats())
        result=dict(status=status,strict_success=strict,fatal=fatal,transactions=transactions,applied_faults=sum(bool(f['complete']) for f in faults),
            replans_completed=len(transactions),replan_attempts=attempts,causal_restart_reached=bool(any(a['status']=='CANCELLED_OBSERVED_MISMATCH' for a in attempts) and transactions),stages=stages,physical_time_s=math.fsum(actual['durations']) if actual else None,
            published_perf=time.perf_counter(),original_path_recovery='NOT_DEMONSTRATED',initial_planner_executed=True,initial_plan_role='UNIFORM_LIVE_INITIAL_PLANNING_FRACTION_0.9')
        if time.perf_counter()>=root['owner_deadline']:result.update(status='OFFLINE_PIPELINE_WALL_TIMEOUT',strict_success=False)
        put(base/'root_result.json',packet(root,'ROOT_RESULT',result))

if __name__=='__main__':main(*sys.argv[1:])
