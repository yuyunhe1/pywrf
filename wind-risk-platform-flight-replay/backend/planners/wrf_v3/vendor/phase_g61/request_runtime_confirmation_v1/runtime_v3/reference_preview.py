"""Compute the same monitor Reference early; it conveys no admission."""
import copy,math,time
from .engine.runtime import common
from phase4t.amendment_v1.model import PlannedRoute,Reference,DomainFail
from phase3b.integrator import SpatialIntegrationError

def build(req,pctx,carrier,us,ds,pnodes,base):
    begin=time.perf_counter();v=common.unpack(req,'UNVALIDATED_SUFFIX_PROPOSAL',common.read(base/'unvalidated_suffix_proposal.json'));suffix=v['trajectory']
    if v['context_id']!=req['suffix_context_id'] or v['trajectory_sha256']!=common.content(suffix):raise common.Stop('REFERENCE_PREVIEW_INPUT_HASH_CONTEXT')
    if suffix['state_start']!=pnodes[-1] or suffix['state_goal']!=carrier['state_goal']:raise common.Stop('REFERENCE_PREVIEW_BOUNDARIES')
    whole=copy.deepcopy(carrier);whole.update(controls=us+suffix['controls'],durations=ds+suffix['durations'],states=pnodes+suffix['states'][1:])
    if math.fsum(whole['durations'])>req['case']['physical_deadline_s']:return None
    try:
        with pctx.evaluation():reference=Reference(PlannedRoute.from_trajectory(whole),pctx.provider,pctx.clock)
    except (DomainFail,SpatialIntegrationError) as exc:
        common.put(base/'speculative_reference_reject.json',common.packet(req,'UNVALIDATED_REFERENCE_REJECT',dict(detail=str(exc),strict_task_success=False)))
        return None
    h=common.content(whole)
    common.put(base/'speculative_reference.json',common.packet(req,'UNVALIDATED_REFERENCE_COMPUTED',dict(
        trajectory_sha256=h,context_id=pctx.id,construction_s=time.perf_counter()-begin,
        status='UNVALIDATED_ONLY_CANNOT_ADMIT',strict_task_success=False)))
    return h,reference
