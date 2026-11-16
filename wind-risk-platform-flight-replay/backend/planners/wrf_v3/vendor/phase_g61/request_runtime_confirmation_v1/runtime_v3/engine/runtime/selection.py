"""Same ordered candidate mathematics; only R1 admits the original terminal lift."""
import copy,math,time
from pathlib import Path
from phase4a_design.mock_contract import content_hash
from .connector import SpatialWindConnector,failed
from .common import put,Stop,Limit,packet
from .c_origin import RecoveryCOrigin
from phase4t_g61.wrf_feasibility_validation_v1.runtime.contract import lift_ok

class Selected(Exception):
    def __init__(self,value):self.value=value

def one_leg(req,ctx,start,goal,base,*,leg='DIRECT'):
    base=Path(base);events=[];chosen=None
    begin=time.perf_counter()
    origin=RecoveryCOrigin(req,ctx,start,goal,leg,base)
    class SelectConnector(SpatialWindConnector):
        def _validation_for_candidate(self,tr,total,index,stage):
            if self.provider is not ctx.provider or self.optimize:raise Stop('C_ORIGIN_ENGINE_SCOPE')
            if req['case']['id']=='P4J-03' and leg=='DIRECT' and not (Path(base).parent/'provisional_route.json').exists():
                put(Path(base).parent/'provisional_route.json',packet(req,'PROVISIONAL_ROUTE',dict(status='UNVALIDATED_PROPOSAL_ONLY',trajectory=tr,trajectory_sha256=content_hash(tr),context_id=ctx.id,physical_offset_s=ctx.offset)))
            if leg=='DIRECT' and not (Path(base).parent/'unvalidated_suffix_proposal.json').exists():
                put(Path(base).parent/'unvalidated_suffix_proposal.json',packet(req,'UNVALIDATED_SUFFIX_PROPOSAL',dict(
                    trajectory=tr,trajectory_sha256=content_hash(tr),context_id=ctx.id,status='UNVALIDATED_ONLY_NOT_ADMISSIBLE')))
            return origin.produce(tr,total,index,stage)
        def _candidate(self,tr,family,stage,validation,index):
            nonlocal chosen
            ctx.clock.check()
            candidate=super()._candidate(tr,family,stage,validation,index)
            t=time.perf_counter()
            if stage=='ENDPOINT_RECOVERY':c=origin.consume(validation,tr,candidate['time'],index,stage)
            else:c=ctx.replay(tr,candidate['time'])
            c_s=time.perf_counter()-t
            t=time.perf_counter();co=ctx.checker.check(tr) if c['valid'] else None;co_s=time.perf_counter()-t
            psi=c.get('diagnostic_final_state',[None,None,None])[2]
            branch=bool(psi is not None and lift_ok(psi,goal[2]))
            accepted=bool(c['valid'] and co and co['valid'] and (branch or req['arm']=='R0_LEGACY_FIRST_VALID'))
            row=dict(leg=leg,seed_index=index,stage=stage,time_s=candidate['time'],C_valid=c['valid'],collision_valid=bool(co and co['valid']),
                terminal_lift=psi,required_lift=goal[2],lift_pass=branch,accepted=accepted,
                reason=None if accepted else 'LIFT_REJECT' if c['valid'] and co and co['valid'] and not branch else 'C_OR_COLLISION_REJECT',
                C_domain_status=c['domain_status'],C_failures=c['failures'],collision_status=co['status'] if co else None,
                elapsed_since_leg_start_s=time.perf_counter()-begin,C_s=c_s,collision_s=co_s,
                context_id=ctx.id,mission_physical_offset_s=ctx.offset)
            row['C_origin_handoff']=copy.deepcopy(origin.last_event) if stage=='ENDPOINT_RECOVERY' else None
            packet=dict(request_sha256=content_hash(req),trajectory=tr,C=c,collision=co,event=row)
            put(base/'candidate_admission'/f'{len(events):03d}_seed_{index}.json',packet)
            events.append(row)
            if c['failures'] and any(f in ('INTERIOR_NODE_RESIDUAL','CONTROL_BOUND') for f in c['failures']):
                raise Stop('WORKER_C_STATE_MISMATCH')
            if accepted:
                chosen=dict(trajectory=copy.deepcopy(tr),C=c,collision=co,events=events,
                    time_s=math.fsum(tr['durations']),selected_seed=index,mission_offset_s=ctx.offset,
                    candidate_found_perf=time.perf_counter())
                raise Selected(chosen)
            return candidate
    engine=SelectConnector(ctx.provider,z_layer_msl_m=ctx.z,optimize=False)
    engine.remaining_physical_s=req['case']['physical_deadline_s']-ctx.offset
    engine.handover_control=req['case']['prefix_policy']['controls'][-1]
    if leg=='DIRECT':engine.reference_seed_route=req['case']['route'];engine.reference_seed_offset=ctx.offset
    try:
        raw=engine.connect(start,goal)
    except Selected as done:
        put(base/'selection.json',done.value);return done.value
    finally:
        put(base/'attempts_snapshot.json',dict(attempts=getattr(engine,'_attempts',[]),reference_warm_attempts=getattr(engine,'_reference_warm_attempts',[]),
            ordered_pool=getattr(engine,'_ordered_seed_pool',[]),candidate_events=events,
            context_id=ctx.id,mission_offset_s=ctx.offset,request_sha256=content_hash(req),
            snapshot_perf=time.perf_counter(),evidence_includes_interrupted_attempt=True))
    put(base/'exhausted.json',dict(raw=raw,events=events,request_sha256=content_hash(req)))
    return None

def _solve_bound(req,ctx,start,base):
    goal=req['case']['route']['state_goal']
    result=one_leg(req,ctx,start,goal,base)
    if result is not None or not req['case']['rectangles']:return result
    # Inherit exactly the existing fixed mask-derived waypoint order and heading.
    from phase4t_g61.remediation_v1.obstacle_route import waypoints
    seed_req=dict(start=dict(x=start[0],y=start[1],heading=start[2]),
        goal=dict(x=goal[0],y=goal[1],heading=goal[2]))
    for i,target in enumerate(waypoints(seed_req,ctx)):
        intermediate=[target['x'],target['y'],target['heading']]
        a=one_leg(req,ctx,start,intermediate,Path(base)/'detours'/str(i)/'leg1',leg='LEG1')
        if a is None:continue
        actual=a['C']['diagnostic_final_state'];offset=ctx.offset+a['time_s']
        bctx=ctx.at_offset(offset)
        b=one_leg(req,bctx,actual,goal,Path(base)/'detours'/str(i)/'leg2',leg='LEG2')
        if b is None:continue
        tr=copy.deepcopy(a['trajectory']);bt=b['trajectory']
        if bt['state_start']!=actual:raise Stop('DETOUR_JOIN_DISCONTINUITY')
        tr.update(state_goal=goal,controls=tr['controls']+bt['controls'],durations=tr['durations']+bt['durations'],states=tr['states']+bt['states'][1:])
        total=math.fsum(tr['durations']);c=ctx.replay(tr,total);co=ctx.checker.check(tr)
        put(Path(base)/'detours'/str(i)/'assembled.json',dict(trajectory=tr,C=c,collision=co,
            first_leg_end=actual,second_leg_clock_offset_s=offset,root_suffix_clock_offset_s=ctx.offset))
        if c['valid'] and co['valid'] and lift_ok(c['diagnostic_final_state'][2],goal[2]):
            return dict(trajectory=tr,C=c,collision=co,time_s=total,selected_waypoint=i,
                        events=a['events']+b['events'],candidate_found_perf=time.perf_counter())
    return None


def solve(req,ctx,start,base):
    with ctx.evaluation():return _solve_bound(req,ctx,start,base)
