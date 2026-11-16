"""Bounded mask-derived two-leg routes; no new steering mathematics or oracle."""
import copy
import math
import time
from pathlib import Path
from phase3a_implementation.domain import VA,R0
from phase4a_design.mock_contract import content_hash

def waypoints(request,context):
    rectangles=context.descriptor['collision']['mask_source']['rectangles']
    if len(rectangles)!=1:raise RuntimeError('ONLY_FROZEN_SINGLE_RECTANGLE_DIAGNOSTIC')
    x0,x1,y0,y1=rectangles[0];radius=context.checker.radius
    clearance=2*R0+radius+1.;cx=(x0+x1)/2;cy=(y0+y1)/2
    points=[(cx,y1+clearance),(cx,y0-clearance),(x0-clearance,cy),(x1+clearance,cy)]
    goal=request['goal'];start=request['start'];out=[]
    for x,y in points:
        heading=math.atan2(goal['y']-y,goal['x']-x)
        lift=heading+2*math.pi*math.floor((start['heading']-heading)/(2*math.pi)+.5)
        out.append(dict(x=x,y=y,z=context.z,heading=lift,time=None))
    return out

def solve_detours(request,context,policy,checkpoint,solve_leg,put):
    base=Path(checkpoint).parent;attempts=[];deadline=request['deadline_perf_s']
    for index,target in enumerate(waypoints(request,context)):
        if time.perf_counter()>=deadline:raise __import__('phase4t.amendment_v1.model',fromlist=['WallLimit']).WallLimit('FIXED_PREPARE_DEADLINE')
        directory=base/'detours'/f'waypoint_{index:02d}';directory.mkdir(parents=True,exist_ok=False)
        leg1=copy.deepcopy(request);leg1.update(request_id=request['request_id']+f'_D{index}_L1',goal=target,required_original_goal_lift_rad=target['heading'])
        out1,raw1=solve_leg(leg1,context,policy,directory/'leg1/checkpoint.json')
        put(directory/'leg1/result.json',dict(edge=out1,raw=raw1))
        record=dict(index=index,target=target,leg1_status=out1['status'],request_sha256=content_hash(request),context_id=context.id)
        if out1['status']!='SUCCESS':attempts.append(record);continue
        actual=out1['validation']['diagnostic_final_state']
        # No snap to requested intermediate target. The second leg starts at the
        # first leg's independently reintegrated actual endpoint and heading.
        leg2=copy.deepcopy(request);leg2.update(request_id=request['request_id']+f'_D{index}_L2',
            start=dict(x=actual[0],y=actual[1],z=context.z,heading=actual[2],time=None))
        out2,raw2=solve_leg(leg2,context,policy,directory/'leg2/checkpoint.json')
        put(directory/'leg2/result.json',dict(edge=out2,raw=raw2));record['leg2_status']=out2['status']
        if out2['status']!='SUCCESS':attempts.append(record);continue
        a=out1['trajectory'];b=out2['trajectory'];tr=copy.deepcopy(a)
        if b['state_start']!=actual:raise RuntimeError('DETOUR_JOIN_SUBSTITUTION')
        tr.update(state_goal=b['state_goal'],controls=a['controls']+b['controls'],durations=a['durations']+b['durations'],states=a['states']+b['states'][1:])
        total=math.fsum(tr['durations']);c=context.replay(tr,total);co=context.checker.check(tr)
        lift=abs(c['diagnostic_final_state'][2]-request['required_original_goal_lift_rad'])<=context.descriptor['C']['thresholds']['heading_rad']
        record.update(full_suffix_C_pass=c['valid'],full_suffix_collision_pass=co['valid'],original_goal_lift_pass=lift,
            no_reset=True,join_from_independent_actual_endpoint=actual,trajectory_sha256=content_hash(tr))
        put(directory/'assembled.json',dict(trajectory=tr,C=c,collision=co,record=record));attempts.append(record)
        if not(c['valid'] and co['valid'] and lift):continue
        if time.perf_counter()>=deadline:raise __import__('phase4t.amendment_v1.model',fromlist=['WallLimit']).WallLimit('DETOUR_VALIDATION_LATE')
        put(base/'detour_admission.json',dict(attempts=attempts,trajectory_sha256=content_hash(tr),C=c,collision=co,
            suffix_only=True,parent_whole_route_and_actual_still_required=True))
        out=copy.deepcopy(out2);out.update(status='SUCCESS',valid=True,trajectory=tr,cost=total,travel_time=total,validation=c,collision=co)
        out['provenance'].update(request=copy.deepcopy(request),request_sha256=content_hash(request),context_id=context.id,
            strategy='BOUNDED_TWO_LEG_MASK_DERIVED_DETOUR',detour_attempts=attempts,
            no_snap_no_oracle=True,global_optimality_guaranteed=False)
        raw=dict(status='SUCCESS',valid=True,trajectory=tr,time=total,validation=c,selection='FIRST_VALID_FIXED_ORDER_TWO_LEG_DETOUR',detour_attempts=attempts)
        return out,raw
    put(base/'detour_exhausted.json',dict(attempts=attempts,scope='FOUR_FIXED_WAYPOINTS_NOT_PHYSICAL_INFEASIBILITY'))
    return None
