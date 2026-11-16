"""External supervisor: transaction ACKs and one unreset 120s root deadline."""
import math,os,subprocess,sys,time,traceback
from pathlib import Path
from .runtime.common import D,ROOT,read,put,sha,content,packet,unpack,source_check,Clock,Stop,Limit,result_schema
from . import contract
from .. import early_ack
from phase4o.ownership import OwnedJob

def acknowledge(root,tx,raw,prechecked=None):
    source_check()
    if (tx/'invalidation.json').exists():return False
    from .runtime.environment import Context
    from phase3b.validator import THRESHOLDS
    from phase4t_g61.wrf_feasibility_validation_v1.runtime.contract import join_ok
    import numpy as np
    req=read(tx/'request.json')
    if prechecked is None:contract.verify_transaction(req)
    else:early_ack.consume(prechecked,req,root,tx,__import__('phase_g61.request_runtime_confirmation_v1.runtime_v3.engine.runtime.common',fromlist=['D']))
    prepared_packet=read(tx/'prepared.json');p=unpack(req,'PREPARED_ROUTE',prepared_packet)
    parent=read(tx/'parent_validation.json');prefix=read(tx/'prefix_assessment.json');case=req['case'];tr=p['trajectory']
    ctx=Context(case,raw,Clock(req['prepare_deadline']))
    if p['context_id']!=ctx.id or req['parent_context_id']!=ctx.id or parent['context_id']!=ctx.id:raise Stop('EXTERNAL_PARENT_CONTEXT')
    if p['full_validation_sha256']!=sha(tx/'parent_validation.json') or p['prefix_assessment_sha256']!=sha(tx/'prefix_assessment.json'):raise Stop('EXTERNAL_PARENT_FILE_CHAIN')
    if content(tr)!=p['trajectory_sha256'] or parent['trajectory_sha256']!=content(tr) or parent['request_sha256']!=content(req):raise Stop('EXTERNAL_PARENT_ROUTE_BINDING')
    from .. import parent_handoff
    certified,origin=parent_handoff.consume(req,tr,ctx,tx,Clock(req['prepare_deadline']))
    if content(certified)!=content(parent['C']):raise Stop('ACK_PARENT_C_BODY_HANDOFF')
    c=parent['C'];co=parent['collision'];goal=root['case']['route']['state_goal']
    if c['valid'] is not True or c['thresholds']!=THRESHOLDS or co['valid'] is not True or abs(c['diagnostic_final_state'][2]-goal[2])>THRESHOLDS['heading_rad']:
        raise Stop('EXTERNAL_PARENT_C_COLLISION_LIFT')
    if tr['state_start']!=case['route']['state_start'] or tr['state_goal']!=goal or math.fsum(tr['durations'])>case['physical_deadline_s']:
        raise Stop('EXTERNAL_PARENT_BOUNDARIES_HORIZON')
    pc=prefix['C_original_goal']
    if any(f not in ('POSITION_RESIDUAL','HEADING_RESIDUAL') for f in pc['failures']) or pc['thresholds']!=THRESHOLDS or not prefix['collision']['valid']:
        raise Stop('EXTERNAL_ACTUAL_PREFIX_VALIDATION')
    prefix_actual=read(tx/'actual_prefix.json')['trajectory']
    root_context=Context(root['case'],raw,Clock(root['owner_deadline']))
    if prefix['actual_prefix_sha256']!=content(prefix_actual) or prefix['root_context_id']!=root_context.id:
        raise Stop('EXTERNAL_ACTUAL_PREFIX_CONTEXT_OR_HASH')
    nodes=np.asarray([row[1:] for row in pc['diagnostic_nodes']]);states=np.asarray(prefix_actual['states'])
    if nodes.shape!=states.shape or np.max(np.linalg.norm(nodes[:,:2]-states[:,:2],axis=1))>8.513422179807296e-6 or np.max(np.abs(nodes[:,2]-states[:,2]))>1e-7:
        raise Stop('EXTERNAL_ACTUAL_PREFIX_NODE_MISMATCH')
    if not join_ok(p['actual_handover_state'],p['predicted_handover_state'])[0] or p['solver_reaped'] is not True:
        raise Stop('EXTERNAL_ACTUAL_JOIN_OR_REAPING')
    solver=unpack(req,'SOLVER_RESULT',read(tx/'solver_result.json'));selected=solver.get('result')
    if solver['status']!='NEW_ROUTE_FOUND' or not selected or selected['C']['valid'] is not True or selected['collision']['valid'] is not True:
        raise Stop('EXTERNAL_WORKER_C_COLLISION')
    if selected['C']['thresholds']!=THRESHOLDS or abs(selected['C']['diagnostic_final_state'][2]-goal[2])>THRESHOLDS['heading_rad']:
        raise Stop('EXTERNAL_WORKER_C_OR_RAW_LIFT')
    if solver['context_id']!=req['suffix_context_id']:raise Stop('EXTERNAL_WORKER_CONTEXT')
    n=len(case['prefix_policy']['controls']);suffix=selected['trajectory']
    if (tr['controls'][:n]!=case['prefix_policy']['controls'] or tr['durations'][:n]!=case['prefix_policy']['durations'] or
        tr['controls'][n:]!=suffix['controls'] or tr['durations'][n:]!=suffix['durations']):
        raise Stop('EXTERNAL_PREFIX_CANDIDATE_BINDING')
    observed=time.perf_counter();accepted=observed<req['prepare_deadline'] and p['published_perf']<req['prepare_deadline']
    ack=packet(req,'PREPARED_ACK',dict(accepted=accepted,prepared_sha256=prepared_packet['sha256'],observed_perf=observed))
    if (tx/'invalidation.json').exists():return False
    put(tx/'prepared_ack.json',ack)
    if accepted and ((tx/'invalidation.json').exists() or time.perf_counter()>=req['prepare_deadline']):
        ack=packet(req,'PREPARED_ACK',dict(accepted=False,prepared_sha256=prepared_packet['sha256'],observed_perf=time.perf_counter()))
        put(tx/'prepared_ack.json',ack,replace=True)

def admission(root,base,value):
    result_schema(value,'OWNER_RESULT')
    if type(value.get('strict_success')) is not bool or type(value.get('fatal')) is not bool:
        raise Stop('ROOT_RESULT_BOOL_SCHEMA')
    if not value['strict_success']:return True
    from phase3b.validator import THRESHOLDS
    from phase4t.execution_v2_20261020.journal_v3 import recover_v3,verify_prefix_against_actual
    if not isinstance(value.get('transactions'),list) or not value['transactions'] or type(value.get('replans_completed')) is not int or value['replans_completed']!=len(value['transactions']):
        raise Stop('ROOT_ONLINE_REPLAN_EVIDENCE_MISSING')
    from ..roles import groups_pass
    if not groups_pass(root['case'],value['transactions']):
        raise Stop('ROOT_REGISTERED_GROUP_REQUIREMENT_NOT_PROVEN')
    a=read(base/'assessment.json');ex=read(base/'executed.json');tr=ex['trajectory'];goal=root['case']['route']['state_goal']
    if value['fatal'] or value['status']!='TASK_SUCCESS' or a['global_C']['valid'] is not True or a['global_collision']['valid'] is not True or a['last_new_route_C']['valid'] is not True:
        raise Stop('ROOT_FALSE_SUCCESS')
    if a['last_new_route_C']['thresholds']!=THRESHOLDS or abs(a['last_new_route_C']['diagnostic_final_state'][2]-goal[2])>THRESHOLDS['heading_rad']:
        raise Stop('ROOT_LAST_C_CONTRACT')
    if a['global_C']['thresholds']!=THRESHOLDS or abs(a['global_C']['diagnostic_final_state'][2]-goal[2])>THRESHOLDS['heading_rad']:
        raise Stop('ROOT_C_RAW_LIFT')
    end=a['global_C']['diagnostic_final_state']
    if math.hypot(end[0]-goal[0],end[1]-goal[1])>8.513422179807296e-6:
        raise Stop('ROOT_C_POSITION_CONTRACT')
    o=a['last_new_route_ordered']
    if o['complete'] is not True or o['assessment_mode']!='LIVE_ORIGINAL_DEADLINE' or o['validation_workers_reaped'] is not True:
        raise Stop('ROOT_LAST_NEW_ROUTE_ORDERED')
    if tr['state_start']!=root['case']['route']['state_start'] or tr['state_goal']!=goal or ex['trajectory_sha256']!=content(tr):raise Stop('ROOT_ACTUAL_IDENTITY')
    if math.fsum(tr['durations'])>root['case']['physical_deadline_s'] or max(map(abs,tr['controls']))>10/8.513422179807296+1e-12:
        raise Stop('ROOT_ACTUAL_PHYSICAL_OR_CONTROL_BOUND')
    prefix=recover_v3(base/'journal',contract.binding(root));verify_prefix_against_actual(prefix,tr,require_complete=True)
    for row in value['transactions']:
        tx=base/'transactions'/f"{row['transaction_index']:02d}"
        if (tx/'invalidation.json').exists():raise Stop('ROOT_INVALIDATED_TRANSACTION')
        req=read(tx/'request.json');contract.verify_transaction(req)
        prepared=read(tx/'prepared.json');commit=unpack(req,'COMMIT',read(tx/'commit.json'));ack=unpack(req,'PREPARED_ACK',read(tx/'prepared_ack.json'))
        if row['prepare_inclusive_s']>=8. or row['accepted'] is not True or row['durable_before_prepare_deadline'] is not True or row['persisted_readback_perf']>=req['prepare_deadline'] or row['intent_perf']!=commit['commit_perf']:
            raise Stop('ROOT_TX_DURABLE_DEADLINE')
        if commit['prepared_sha256']!=prepared['sha256'] or ack['accepted'] is not True or ack['prepared_sha256']!=prepared['sha256'] or ack['observed_perf']>=req['prepare_deadline']:
            raise Stop('ROOT_TX_RECEIPT_CHAIN')
    return True

def main(execute=False,run_id=None):
    if not execute:raise Stop('EXPLICIT_EXECUTE_REQUIRED')
    begin=float(read(D.parent/'PIPELINE_REQUEST.json')['start_perf']);source_check();manifest=read(D.parent/'ROOT_INPUT_MANIFEST_V2.json');freeze=read(D/'RUNTIME_FREEZE.json')
    early_inputs={}
    row=next(r for r in manifest['order'] if r['run_id']==run_id);case=next(c for c in manifest['cases'] if c['id']==row['case_id']);base=D/'runs'/row['run_id'];base.mkdir(parents=True,exist_ok=False)
    root=dict(version='G61_MIDFLIGHT_ROOT_V1',run_id=row['run_id'],arm=row['arm'],case=case,
        nonce=freeze['nonces'][row['run_id']],epoch=freeze['epoch'],runtime_freeze_sha256=sha(D/'RUNTIME_FREEZE.json'),
        owner_start=begin,owner_deadline=begin+120.)
    put(base/'request.json',root);contract.verify_root(root);process=job=None;value=None;external=[]
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with (base/'stdout.txt').open('x') as out,(base/'stderr.txt').open('x') as err:
        process=subprocess.Popen([sys.executable,'-X','utf8','-B','-m','phase_g61.request_runtime_confirmation_v1.runtime_v3.engine.controller',str(base/'request.json'),str(base)],cwd=ROOT,stdout=out,stderr=err,creationflags=flags)
    job=OwnedJob(process)
    try:
        # Required supervisor source/context preparation is inside the root
        # task's setup budget and overlaps the fresh controller's preparation.
        from .runtime.environment import load
        raw=load(case)
        while process.poll() is None:
            for tx in sorted((base/'transactions').glob('*')) if (base/'transactions').exists() else []:
                if (tx/'request.json').exists() and not (tx/'invalidation.json').exists() and tx.name not in early_inputs:
                    t=time.perf_counter();request=read(tx/'request.json')
                    common=__import__('phase_g61.request_runtime_confirmation_v1.runtime_v3.engine.runtime.common',fromlist=['D'])
                    early_inputs[tx.name]=early_ack.prepare(request,root,tx,common,contract)
                    external.append(dict(transaction=tx.name,early_full_input_verification_s=time.perf_counter()-t,
                        verification_finished_perf=early_inputs[tx.name]['verified_perf'],inside_original_prepare_deadline=True))
                if (tx/'prepared.json').exists() and not (tx/'invalidation.json').exists() and not (tx/'prepared_ack.json').exists():
                    t=time.perf_counter();acknowledge(root,tx,raw,early_inputs[tx.name]);external.append(dict(transaction=tx.name,external_ACK_s=time.perf_counter()-t))
            if time.perf_counter()>=root['owner_deadline']:raise TimeoutError('GLOBAL_120S')
            time.sleep(.002)
        if process.returncode!=0:raise Stop('ROOT_CONTROLLER_NONZERO')
    except TimeoutError:
        value=dict(status='OFFLINE_PIPELINE_WALL_TIMEOUT',strict_success=False,fatal=False,detail='GLOBAL_SUPERVISOR_120S')
    except Limit as exc:
        value=dict(status='REPLAN_BUDGET_EXCEEDED',strict_success=False,fatal=False,detail='EARLY_OR_FINAL_ACK_ORIGINAL8S:'+str(exc))
    except BaseException as exc:
        value=dict(status='IPC_OR_SCHEMA_STOP',strict_success=False,fatal=True,detail=type(exc).__name__+': '+str(exc),traceback=traceback.format_exc())
    finally:
        t=time.perf_counter();job.close();cleanup=time.perf_counter()-t
    if value is None:
        try:value=unpack(root,'ROOT_RESULT',read(base/'root_result.json'));admission(root,base,value)
        except BaseException as exc:value=dict(status='IPC_OR_SCHEMA_STOP',strict_success=False,fatal=True,detail=type(exc).__name__+': '+str(exc),traceback=traceback.format_exc())
    source_check();files={p.relative_to(base).as_posix():sha(p) for p in base.rglob('*') if p.is_file() and p.name!='completion.json'}
    comp=dict(value,receipt_phase='SEALED',root_request_sha256=content(root),root_context_input_sha256=content(case),
        nonce=root['nonce'],epoch=root['epoch'],root_worker_reaped=True,artifact_sha256=files,
        external_ACK_work=external,cleanup_s=cleanup,complete_inclusive_wall_s=time.perf_counter()-begin)
    if time.perf_counter()>=root['owner_deadline']:comp.update(status='OFFLINE_PIPELINE_WALL_TIMEOUT',strict_success=False)
    put(base/'completion.json',comp)
    if read(base/'completion.json')!=comp:raise Stop('ROOT_SEALED_READBACK')
    final=time.perf_counter()-begin
    if final>=120.:
        comp.update(status='OFFLINE_PIPELINE_WALL_TIMEOUT',strict_success=False,complete_inclusive_wall_s=final);put(base/'completion.json',comp,replace=True)
    put(D/'summaries'/(row['run_id']+'.json'),dict(status=comp['status'],strict_success=comp['strict_success'],fatal=comp['fatal'],
        root_run_id=row['run_id'],completion_sha256=sha(base/'completion.json'),inclusive_wall_s=final,
        physical_time_s=comp.get('physical_time_s'),replans_completed=comp.get('replans_completed'),
        applied_faults=comp.get('applied_faults'),global_offline_cap_s=120.))
    print('MIDFLIGHT_PILOT',comp['status'],comp['strict_success'],'replans',comp.get('replans_completed'),round(final,3),flush=True)

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');parser.add_argument('--run-id',required=True);args=parser.parse_args();main(args.execute,args.run_id)
