"""Commit is accepted only after its durable complete readback before prepare end."""
import time
from .common import put,packet,Limit,Stop

def publish_commit(req,base,prepared_sha256,actual_handover_state,join,prepare):
    if (base/'invalidation.json').exists():raise Stop('COMMIT_OF_INVALIDATED_ATTEMPT')
    if prepare.deadline!=req['prepare_deadline']:raise Stop('COMMIT_DEADLINE_IDENTITY')
    prepare.check();intent=time.perf_counter()
    put(base/'commit.json',packet(req,'COMMIT',dict(prepared_sha256=prepared_sha256,
        commit_perf=intent,actual_handover_state=actual_handover_state,join=join)))
    persisted=time.perf_counter()
    try:prepare.check();accepted=True
    except Limit:accepted=False
    invalidated=(base/'invalidation.json').exists()
    if invalidated:accepted=False
    return dict(accepted=accepted,intent_perf=intent,persisted_readback_perf=persisted,
        durable_before_prepare_deadline=persisted<req['prepare_deadline'],invalidated_at_readback=invalidated)

def deadline_failure(req,now,commit_accepted):
    if now>=req['owner_deadline']:return 'OFFLINE_PIPELINE_WALL_TIMEOUT',False
    if not commit_accepted:return 'REPLAN_BUDGET_EXCEEDED',False
    # A prepare-clock escape after an accepted commit is an implementation
    # error, not evidence that a still-unexpired owner clock timed out.
    return 'STRUCTURAL_STOP',True
