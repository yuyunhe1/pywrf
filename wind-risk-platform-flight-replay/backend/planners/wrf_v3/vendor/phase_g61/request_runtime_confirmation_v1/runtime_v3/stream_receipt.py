"""Complete same-prefix snapshots and fresh source identity inside original8s."""
import copy,time
from pathlib import Path
from .work import read,sha,content
from .engine.runtime import common
from .engine.runtime.environment import Context,load
from .prefix_math import prefix_dynamics


def publish(req,root,stream,actual,context_id,tx,clock,at_trigger):
    begin=time.perf_counter();clock.check();common.source_check()
    # Full original NC entry/exit, NPZ and per-array identity is still paid in8s.
    fresh=load(root['case']);fresh_context=Context(root['case'],fresh,clock)
    if fresh_context.id!=context_id or stream.Cctx.id!=context_id or stream.COctx.id!=context_id:
        raise common.Stop('STREAM_ROOT_CONTEXT_IDENTITY')
    if content(stream.trace)!=content(actual):raise common.Stop('STREAM_ACTUAL_PREFIX_IDENTITY')
    value=stream.snapshot();prefix_dynamics(value['C'],actual,common.Stop)
    if not value['collision']['valid']:raise common.Stop('STREAM_COLLISION_FAILED_SNAPSHOT')
    for role,key in [('C','C'),('COLLISION','collision')]:
        clock.check();payload=dict(status='PASS',role=role,value=value[key],request_sha256=content(req),actual_prefix_sha256=content(actual),
            root_context_id=context_id,strict_task_success=False,diagnostic_only=False,published_perf=time.perf_counter(),
            computation_schedule='ORIGINAL_ROW_REPLAY_AFTER_DURABLE_ACTUAL_RECORD_NOT_COLD_REPLAY_AT_TRIGGER',
            prefix_stream_statistics=stream.statistics(),prefix_stream_statistics_at_first_trigger=at_trigger,
            fresh_source_context_verified_inside8s=True,snapshot_inclusive_s=time.perf_counter()-begin)
        path=Path(tx)/('prefix_'+role+'_result.json');common.put(path,common.packet(req,'PREFIX_'+role,payload));persisted=time.perf_counter()
        common.put(Path(tx)/('prefix_'+role+'_publication.json'),common.packet(req,'PREFIX_'+role+'_PUBLICATION',dict(
            result_file_sha256=sha(path),persisted_readback_perf=persisted,prepare_deadline=req['prepare_deadline'],timely=persisted<req['prepare_deadline'],role=role)))
    clock.check()
