import copy,math,os,subprocess,sys,time,uuid
from pathlib import Path
from .work import D,ROOT,read,sha,content,put,check,remaining,BOUND
class SourceTimeReject(Exception):pass

def worker(request_path,base_path):
    req=read(request_path);base=Path(base_path);begin=req['start_perf'];clock_end=req['deadline_perf'];status='INITIAL_NO_VALID_ROUTE';result=None;errors=[]
    if req.get('start_perf')!=BOUND['paid_start_perf'] or req.get('deadline_perf')!=BOUND['paid_start_perf']+20. or req.get('initial_planner_deadline_perf')!=BOUND['paid_start_perf']+60. or req.get('root_deadline_perf')!=BOUND['paid_start_perf']+120.:raise RuntimeError('INITIAL_REQUEST_LAYOUT_CLOCK')
    freeze_path=Path(req['preplan_freeze_file']).resolve()
    if not freeze_path.is_relative_to(D):raise RuntimeError('PREPLAN_PATH_ESCAPE')
    frozen=read(freeze_path)
    if frozen['mission_case_sha256']!=content(req['case']):raise RuntimeError('INITIAL_MISSION_CASE_BINDING')
    if req['preplan_freeze_sha256']!=sha(freeze_path):raise RuntimeError('PREPLAN_FREEZE_IDENTITY')
    if time.perf_counter()>=clock_end:
        put(base/'result.json',dict(status='INITIAL_PLANNING_TIMEOUT',result=None,errors=[dict(detail='ENTRY_ALREADY_EXPIRED')],request_sha256=content(req),nonce=req['nonce'],published_perf=time.perf_counter(),wall_s=time.perf_counter()-begin,mathematical_search_entered=False,new_physical_executions=0,context_id=None,query_counters=None,source_checks_complete=False))
        return
    for path,h in frozen['source_sha256'].items():
        if sha(ROOT/path)!=h:raise RuntimeError('INITIAL_PLANNER_SOURCE_CHANGED')
    try:
        from phase_g61.uncovered_environments_v1.enclosed_v3.runtime.environment import load,Context
        from phase_g61.uncovered_environments_v1.enclosed_v3.runtime.common import Clock
        from phase_g61.uncovered_environments_v1.enclosed_v3.runtime import connector as backend
        from phase_g61.uncovered_environments_v1.warm_recovery import trial
        from phase_g61.uncovered_environments_v1.initial_seed import reserve_seed,waypoint_order,RESERVE_FRACTION
        from phase2b4.solver_a.general_wind_connector import GeneralWindConnector
        from phase3a_implementation.synthetic import ConstantWindProvider
        from phase3b.validator import validate
        import numpy as np
        RESERVE_FRACTION=req['case']['initial_reserve_fraction']
        if isinstance(RESERVE_FRACTION,bool) or RESERVE_FRACTION not in (.9,1.):raise ValueError('FROZEN_INITIAL_RESERVE_ARM')
        case=req['case'];clock=Clock(clock_end);raw=load(case);ctx=Context(case,raw,clock);start=case['route']['state_start'];goal=case['route']['state_goal'];clock.check()
        mathematical_search_entered=False
        q=ctx.provider.wind_query(start[0],start[1],ctx.z,0.)
        if not q.get('valid') or math.hypot(q['wx'],q['wy'])>=10.:raise ValueError('INITIAL_START_DOMAIN_REJECT')
        from phase_g61.request_runtime_confirmation_v1.runtime_v3.time_coverage_guard import evaluate
        proof=evaluate(case,raw.source)
        put(base/'source_time_coverage.json',dict(request_sha256=content(req),nonce=req['nonce'],context_id=ctx.id,context_descriptor=ctx.descriptor,proof=proof))
        if proof['decision']=='REJECT_SOURCE_TIME_COVERAGE':raise SourceTimeReject('SOURCE_TIME_COVERAGE_SHORTER_THAN_NECESSARY_POSITION_TIME')
        mathematical_search_entered=True
        wind=[q['wx'],q['wy']];constant=ConstantWindProvider(*wind,0.,roi=ctx.provider.roi);connector=GeneralWindConnector();seed_events=[]
        class Selected(Exception):
            def __init__(self,value):self.value=value
        def constant_leg(a,b,label):
            clock.check();pool=connector.connect(a,b,wind,{'Va':10.,'R0':8.513422179807296});events=[]
            for i,seed in enumerate(pool.get('candidates',[])):
                clock.check();tr=seed.get('trajectory')
                if not tr:continue
                tr=copy.deepcopy(tr);tr.update(z_layer_msl_m=ctx.z,frame_id=ctx.provider.frame_id,height_datum=ctx.provider.height_datum)
                branch=abs(tr['states'][-1][2]-b[2])<=1e-7
                if not branch:events.append(dict(index=i,status='CONSTANT_SEED_LIFT_REJECT'));continue
                c=validate(tr,tr['controls'],math.fsum(tr['durations']),constant)
                events.append(dict(index=i,status='CONSTANT_SEED_C_PASS' if c['valid'] else 'CONSTANT_SEED_C_REJECT'))
                if c['valid']:
                    put(base/f'{label}.json',dict(pool=pool,events=events,selected_index=i,C=c,raw_lift_required=b[2]));return tr,c
            put(base/f'{label}.json',dict(pool=pool,events=events,selected_index=None));return None,None
        class Engine(backend.SpatialWindConnector):
            def _validation_for_candidate(self,tr,total,index,stage):return ctx.replay(tr,total)
            def _candidate(self,tr,family,stage,c,index):
                co=ctx.checker.check(tr);data=dict(trajectory=tr,C=c,collision=co,index=index,scope='INITIAL_ROUTE_NOT_EXECUTED_TASK',reserve_fraction=RESERVE_FRACTION,
                    planned_peak_turn_rate=max(abs(u) for u in tr['controls']),physical_turn_rate_limit=10/8.513422179807296)
                put(base/f'candidate_{index:02d}.json',data)
                reserve_ok=max(abs(u) for u in tr['controls'])<=RESERVE_FRACTION*(10/8.513422179807296)+1e-12
                if reserve_ok and c['valid'] and co['valid'] and abs(c['diagnostic_final_state'][2]-goal[2])<=1e-7 and math.fsum(tr['durations'])<=case['initial_physical_cap_s']:raise Selected(data)
        if not case['rectangles']:
            pool=connector.connect(start,goal,wind,{'Va':10.,'R0':8.513422179807296})
            put(base/'direct_ordered_pool.json',dict(pool=pool,request_sha256=content(req)))
            first_valid_hint=None
            for index,item in enumerate(pool.get('candidates',[])):
                clock.check();original=copy.deepcopy(item.get('trajectory'));event=dict(index=index,status='STARTED')
                if not original or abs(original['states'][-1][2]-goal[2])>1e-7:
                    put(base/f'direct_seed_{index:02d}.json',dict(status='SEED_RAW_LIFT_REJECT',seed=item));continue
                original.update(z_layer_msl_m=ctx.z,frame_id=ctx.provider.frame_id,height_datum=ctx.provider.height_datum)
                constant_C=validate(original,original['controls'],math.fsum(original['durations']),constant)
                if not constant_C['valid']:
                    put(base/f'direct_seed_{index:02d}.json',dict(status='SEED_CONSTANT_C_REJECT',seed=item,C=constant_C));continue
                seed=reserve_seed(original,fraction=RESERVE_FRACTION);H=max(20.,4*math.fsum(seed['durations'])+10.)
                if first_valid_hint is None:first_valid_hint=math.fsum(seed['durations'])
                engine=Engine(ctx.provider,z_layer_msl_m=ctx.z,optimize=False);engine.reference_seed_route=seed;engine.reference_seed_offset=0.;engine.remaining_physical_s=min(H,case['initial_physical_cap_s'])
                try:
                    with ctx.evaluation():
                        if all(d>0 for d in seed['durations']) and all(u==0. for u in seed['controls']):
                            direct=engine._trajectory(np.asarray(start),np.asarray(goal),seed['controls'],seed['durations']);C=ctx.replay(direct,math.fsum(direct['durations']))
                            if C['valid']:engine._candidate(direct,'POSITIVE_STRAIGHT_INITIAL','ENDPOINT_RECOVERY',C,index)
                        trial(engine,np.asarray(start),np.asarray(goal),backend,index)
                except Selected as found:result=found.value;status='INITIAL_PLAN_FOUND';break
                finally:put(base/f'direct_root_{index:02d}.json',dict(seed=seed,constant_C=constant_C,attempts=getattr(engine,'_reference_warm_attempts',[])))
            if result is None and first_valid_hint is not None:
                clock.check()
                from .initial_two_zoh import attempt as bounded_initial_attempt
                engine=Engine(ctx.provider,z_layer_msl_m=ctx.z,optimize=False)
                try:
                    with ctx.evaluation():bounded_initial_attempt(engine,np.asarray(start),np.asarray(goal),backend,
                        len(pool.get('candidates',[]))+1,first_valid_hint,case['initial_physical_cap_s'],RESERVE_FRACTION)
                except Selected as found:result=found.value;status='INITIAL_PLAN_FOUND'
                finally:put(base/'bounded_two_zoh.json',getattr(engine,'_initial_two_zoh_attempt',dict(status='NOT_STARTED')))
        else:
            for index,target in enumerate(waypoint_order(case)):
                clock.check();a,ac=constant_leg(start,target,f'seed_{index}_leg1')
                if a is None:continue
                handover=ac['diagnostic_final_state'];b,bc=constant_leg(handover,goal,f'seed_{index}_leg2')
                if b is None:continue
                combined=copy.deepcopy(a);combined.update(state_goal=goal,controls=a['controls']+b['controls'],durations=a['durations']+b['durations'],states=a['states']+b['states'][1:])
                seed=reserve_seed(combined,fraction=RESERVE_FRACTION);H=max(20.,4*math.fsum(seed['durations'])+10.)
                seed_events.append(dict(index=index,target=target,source='LIVE_CONSTANT_WIND_TWO_LEG_CONTROL_SEED_NOT_SPATIAL_ROUTE_ORACLE',numeric_search_horizon_s=H,
                    physical_execution_deadline='NOT_DEFINED_UNTIL_FIRST_VALID_INITIAL_ROUTE_FROZEN',seed_sha256=content(seed)))
                engine=Engine(ctx.provider,z_layer_msl_m=ctx.z,optimize=False);engine.reference_seed_route=seed;engine.reference_seed_offset=0.;engine.remaining_physical_s=H
                try:
                    with ctx.evaluation():trial(engine,np.asarray(start),np.asarray(goal),backend,index)
                except Selected as found:result=found.value;status='INITIAL_PLAN_FOUND';break
                finally:put(base/f'root_{index:02d}.json',dict(seed=seed,event=seed_events[-1],attempts=getattr(engine,'_reference_warm_attempts',[])))
        if result is not None:
            put(base/'pre_time_refinement_incumbent.json',result)
            arm=case['initial_time_arm']
            if arm=='F1':
                from .time_refinement_v4 import refine
                result=refine(result,ctx,req,base)
            elif arm!='F0':raise ValueError('UNREGISTERED_TIME_ARM')
        clock.check()
    except SourceTimeReject as exc:
        status='INITIAL_TIME_DOMAIN_REJECT';errors.append(dict(type=type(exc).__name__,detail=str(exc),coverage_proof_sha256=content(proof)))
    except Exception as exc:
        status='INITIAL_PLANNING_TIMEOUT' if type(exc).__name__=='Limit' else 'INITIAL_PLANNING_ERROR';errors.append(dict(type=type(exc).__name__,detail=str(exc)))
    if time.perf_counter()>=clock_end:status='INITIAL_PLANNING_TIMEOUT';result=None
    payload=dict(status=status,result=result,errors=errors,request_sha256=content(req),nonce=req['nonce'],published_perf=time.perf_counter(),wall_s=time.perf_counter()-begin,
        mathematical_search_entered=locals().get('mathematical_search_entered',False),new_physical_executions=0,context_id=ctx.id if 'ctx' in locals() else None,query_counters=raw.query_stats() if 'raw' in locals() else None)
    put(base/'result.json',payload)
if __name__=='__main__':worker(*sys.argv[1:])
