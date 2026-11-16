"""Parent-owned original full C in the existing single-use solver process."""
import copy,math,time,traceback
from pathlib import Path
from .engine.runtime import common
from .engine.runtime.environment import Context
from .prefix_receipt import validate

def submit(req,whole,ctx,base):
    ctx.assert_bound()
    if ctx.id!=req['parent_context_id']:raise common.Stop('PARENT_C_SUBMIT_CONTEXT')
    common.put(base/'parent_C_input.json',common.packet(req,'PARENT_FULL_C_INPUT',dict(
        trajectory=whole,trajectory_sha256=common.content(whole),context_id=ctx.id,
        source='OWNER_EXACT_JOINED_PATH_AFTER_OBSERVED_HANDOVER_NOT_APPROXIMATE_PROPOSAL')))

def produce(req,raw,selected,base,clock):
    begin=time.perf_counter();payload=None
    try:
        while not (base/'parent_C_input.json').exists():clock.check();time.sleep(.002)
        data=common.unpack(req,'PARENT_FULL_C_INPUT',common.read(base/'parent_C_input.json'));tr=data['trajectory']
        inp=common.unpack(req,'SOLVER_INPUT',common.read(base/'solver_input.json'));suffix=selected['trajectory'];p=req['case']['prefix_policy']
        expected=copy.deepcopy(req['case']['route']);expected.update(controls=p['controls']+suffix['controls'],durations=p['durations']+suffix['durations'],
            states=inp['prefix_nodes']+suffix['states'][1:])
        if common.content(expected)!=common.content(tr) or data['trajectory_sha256']!=common.content(tr):raise common.Stop('PARENT_C_INPUT_TRAJECTORY')
        if inp['prefix_nodes'][0]!=req['case']['route']['state_start'] or inp['prefix_nodes'][-1]!=suffix['state_start']:
            raise common.Stop('PARENT_C_INPUT_PREFIX_BOUNDARY')
        common.source_check();ctx=Context(req['case'],raw,clock,0.)
        if ctx.id!=data['context_id'] or ctx.id!=req['parent_context_id']:raise common.Stop('PARENT_C_PRODUCER_CONTEXT')
        t=time.perf_counter();c=ctx.replay(tr,math.fsum(tr['durations']));finished=time.perf_counter()
        payload=dict(status='PASS' if c['valid'] else 'C_REJECT',role='PARENT_C',diagnostic_only=False,value=c,
            context_id=ctx.id,trajectory_sha256=common.content(tr),published_perf=time.perf_counter(),
            C_started_perf=t,C_finished_perf=finished,C_s=finished-t,worker_entry_inclusive_s=finished-begin,
            producer='EXISTING_SINGLE_USE_SOLVER_PROCESS_AFTER_SUFFIX_RESULT',original_validator_sha256=common.sha(common.ROOT/'phase3b/validator.py'),
            full_route_no_reset_from_actual_anchor=True,strict_task_success=False)
    except common.Limit as exc:
        payload=dict(status='LIMIT',role='PARENT_C',diagnostic_only=False,detail=str(exc),published_perf=time.perf_counter())
    except BaseException as exc:
        payload=dict(status='STRUCTURAL_STOP',role='PARENT_C',diagnostic_only=False,detail=type(exc).__name__+':'+str(exc),traceback=traceback.format_exc(),published_perf=time.perf_counter())
    path=base/'parent_C_result.json';common.put(path,common.packet(req,'PARENT_FULL_C_RESULT',payload));persisted=time.perf_counter()
    common.put(base/'parent_C_publication.json',common.packet(req,'PARENT_FULL_C_PUBLICATION',dict(role='PARENT_C',
        result_file_sha256=common.sha(path),persisted_readback_perf=persisted,prepare_deadline=req['prepare_deadline'],timely=persisted<req['prepare_deadline'])))
    if hasattr(raw,'query_stats'):common.put(base/'parent_C_query_stats.json',common.packet(req,'PARENT_C_QUERY_STATS',raw.query_stats()))

def consume(req,whole,ctx,base,clock):
    clock.check();path=base/'parent_C_result.json';v=common.unpack(req,'PARENT_FULL_C_RESULT',common.read(path))
    publication=common.unpack(req,'PARENT_FULL_C_PUBLICATION',common.read(base/'parent_C_publication.json'))
    validate(v,publication,req,'PARENT_C',common.sha(path),time.perf_counter(),clock.deadline,common.Stop,common.Limit)
    if v['status']=='LIMIT':raise common.Limit('PARENT_FULL_C_DEADLINE')
    if v['status'] not in ('PASS','C_REJECT'):raise common.Stop('PARENT_FULL_C_WORKER:'+v.get('detail',''))
    ctx.assert_bound()
    if v['context_id']!=ctx.id or ctx.id!=req['parent_context_id'] or v['trajectory_sha256']!=common.content(whole):raise common.Stop('PARENT_FULL_C_HANDOFF_IDENTITY')
    if v['original_validator_sha256']!=common.sha(common.ROOT/'phase3b/validator.py') or not v['full_route_no_reset_from_actual_anchor']:
        raise common.Stop('PARENT_FULL_C_ORIGINAL_CALLBACK')
    return v['value'],v
