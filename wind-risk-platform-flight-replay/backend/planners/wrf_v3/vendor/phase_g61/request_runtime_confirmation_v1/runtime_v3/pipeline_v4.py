"""One paid live initial plan -> local recovery -> original exact terminal task."""
import copy,math,os,sys,time,traceback,uuid
from pathlib import Path
from .work import D,ROOT,read,put,sha,content,check,remaining,BOUND

def main(paid_start,case_key):
    check();relative='tasks/'+case_key;base=D/relative;out=base/'initial_plan';mi=read(base/'MISSION_INPUT.json');f=read(base/'PREPLAN_FREEZE.json')
    from .registry import require_input
    require_input(case_key,mi)
    if os.environ.get('G61_CAUSAL_ENV_CASE')!=case_key:raise RuntimeError('RUNTIME_CASE_ENV_IDENTITY')
    if (base/'PIPELINE_REQUEST.json').exists():raise RuntimeError('REFUSE_REPEAT_LIVE_STRONG_INPUT')
    if float(paid_start)!=BOUND['paid_start_perf']:raise RuntimeError('REQUEST_PAID_START_RESET')
    begin=float(paid_start);stages={};status='INITIAL_NO_VALID_ROUTE';fatal=False;flight=False;job=None;summary=None
    req=dict(stage='INITIAL_PLANNING',case=mi['case'],nonce=uuid.uuid4().hex,start_perf=begin,deadline_perf=begin+20.,
        initial_planner_deadline_perf=begin+60.,root_deadline_perf=begin+120.,preplan_freeze_file=str(base/'PREPLAN_FREEZE.json'),preplan_freeze_sha256=sha(base/'PREPLAN_FREEZE.json'))
    put(relative+'/PIPELINE_REQUEST.json',req);out.mkdir(exist_ok=False);put(relative+'/initial_plan/request.json',req)
    try:
        from phase4o.ownership import OwnedJob
        from .checked_admission import admit_initial,InitialOrdinaryFailure
        import subprocess
        t=time.perf_counter()
        for path,h in f['source_sha256'].items():
            if sha(ROOT/path)!=h:raise RuntimeError('PREPLAN_SOURCE_CHANGED:'+path)
        stages['preplan_source_check_s']=time.perf_counter()-t
        with (out/'stdout.txt').open('x') as stdout,(out/'stderr.txt').open('x') as stderr:
            process=subprocess.Popen([sys.executable,'-X','utf8','-B','-m','phase_g61.request_runtime_confirmation_v1.runtime_v3.initial_worker_v4',str(out/'request.json'),str(out)],
                cwd=ROOT,stdout=stdout,stderr=stderr,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        job=OwnedJob(process)
        try:process.wait(timeout=max(.001,req['deadline_perf']-time.perf_counter()))
        except subprocess.TimeoutExpired:pass
        finally:job.close();job=None
        stages['initial_worker_inclusive_from_paid_start_s']=time.perf_counter()-begin
        if process.returncode!=0 or not (out/'result.json').exists():
            if time.perf_counter()<req['deadline_perf']:raise RuntimeError('INITIAL_WORKER_EARLY_CRASH_OR_SCHEMA')
            raise InitialOrdinaryFailure('INITIAL_WORKER_TIMEOUT_OR_NO_RESULT')
        saved=read(out/'result.json');coverage=read(out/'source_time_coverage.json') if saved.get('status')=='INITIAL_TIME_DOMAIN_REJECT' else None
        tr=admit_initial(req,saved,time.perf_counter(),coverage_receipt=coverage);T=math.fsum(tr['durations'])
        from .terminal_policy import derive
        try:terminal=derive(tr)
        except ValueError as exc:raise InitialOrdinaryFailure('INITIAL_ROUTE_NOT_SUPPORTED_BY_FROZEN_TERMINAL_POLICY:'+str(exc)) from exc
        from .roles import private_input
        from .roles import eligibility
        if not eligibility(mi['case'],T):raise InitialOrdinaryFailure('REGISTERED_MISSION_ROLE_NOT_ELIGIBLE')
        private=private_input(mi,terminal)
        put(relative+'/PLANT_INPUT.json',private)
        case=copy.deepcopy(mi['case']);case.update(id='CAUSALENV_'+case_key,route=tr,T_ref_s=T,
            legacy_physical_deadline_s=mi['legacy_physical_cap_s'],physical_deadline_s=min(2*T+10.,mi['legacy_physical_cap_s']),
            original_J_seed=mi['original_J_seed'],private_plant_sha256=sha(base/'PLANT_INPUT.json'),
            causal_prepare_policy=dict(maximum_groups=2,maximum_attempts=2,original_prepare_s=8.,restart='FIRST_OBSERVED_NORMAL_AFTER_ACTUATOR_MISMATCH'),trigger=dict(position_error_m=1.,heading_error_rad=.02,logic='OR',maximum_replans=2),
            coverage_season=mi['season'],coverage_role='CAUSAL_PREPARATION_SECOND_DISTURBANCE',
            local_recovery_policy=dict(version='TIME_CONSISTENT_CURVE_CAPTURE_V4',native_interval_local_s=__import__('phase_g61.request_runtime_confirmation_v1.runtime_v3.native_clock',fromlist=['coverage_from_spec']).coverage_from_spec(case['provider']),terminal_switch_rule=terminal['rule'],
                terminal_switch_physical_s=terminal['trigger_physical_s'],terminal_suffix_policy=terminal,no_feedback_after_validated_handover=True,only_one_registered_fault=False))
        case['prefix_evidence_policy']='ORIGINAL_ROW_REPLAY_ON_DURABLE_ACTUAL_HISTORY_V1'
        case['required_committed_groups']=mi['case']['mission_role']['minimum_committed_groups']
        from .engine.runtime.environment import Context,load
        from .engine.runtime.common import Clock
        from .engine.contract import prefix_controls
        t=time.perf_counter();ctx=Context(case,load(case),Clock(req['initial_planner_deadline_perf']));stages['initial_parent_setup_s']=time.perf_counter()-t
        t=time.perf_counter();c=ctx.replay(tr,T);co=ctx.checker.check(tr);stages['initial_parent_C_collision_s']=time.perf_counter()-t
        if not c['valid'] or not co['valid'] or abs(c['diagnostic_final_state'][2]-tr['state_goal'][2])>1e-7:raise RuntimeError('INITIAL_PARENT_C_COLLISION_LIFT_MISMATCH')
        put(relative+'/initial_plan/frozen_planned_route.json',tr)
        put(relative+'/initial_plan/parent_case.json',case)
        put(relative+'/initial_plan/parent_validation.json',dict(C=c,collision=co,trajectory_sha256=content(tr),request_sha256=content(req),context_id=ctx.id,
            context_case_sha256=content(case),planned_route_only_not_executed_task=True))
        case.update(prefix_policy=prefix_controls(tr),initial_plan_source=dict(route_file=(out/'frozen_planned_route.json').relative_to(ROOT).as_posix(),route_file_sha256=sha(out/'frozen_planned_route.json'),
            parent_receipt_file=(out/'parent_validation.json').relative_to(ROOT).as_posix(),parent_receipt_sha256=sha(out/'parent_validation.json'),
            pipeline_request_file=(base/'PIPELINE_REQUEST.json').relative_to(ROOT).as_posix(),pipeline_request_sha256=sha(base/'PIPELINE_REQUEST.json'),
            planner_identity=('WIND_AWARE_FIRST_FEASIBLE_RESERVE_0.9' if mi['time_arm']=='F0' else 'WIND_AWARE_FIRST_FEASIBLE_THEN_BOUNDED_TIME_V4_RESERVE_0.9'),source_case_id=mi['source_case_id'],actual_controller_output_used_as_reference=False),
            source_planned_route_identity=dict(current_live_initial_planning=True,plan_input_task_not_new_live_initial_planning=False,original_mission_sha256=sha(base/'MISSION_INPUT.json')))
        runid='G61CAUSALENV_'+case_key;m=dict(version='LIVE_STRONG_INITIAL_PLUS_LOCAL_RECOVERY',cases=[case],order=[dict(run_id=runid,case_id=case['id'],arm='R18_MIDFLIGHT_EVENT')],
            initial_plan_paid_in_global_wall=True,initial_worker_s=20.,initial_planner_s=60.,global_offline_wall_s=120.,per_replan_prepare_s=8.)
        put(relative+'/ROOT_INPUT_MANIFEST_V2.json',m)
        rf=copy.deepcopy(f);rf.update(manifest_sha256=sha(base/'ROOT_INPUT_MANIFEST_V2.json'),order=m['order'],epoch='G61UNIFORM_'+uuid.uuid4().hex,
            nonces={runid:uuid.uuid4().hex},not_new_live_initial_plan=False)
        for p in [base/'PLANT_INPUT.json',base/'PIPELINE_REQUEST.json',base/'ROOT_INPUT_MANIFEST_V2.json',out/'result.json',out/'frozen_planned_route.json',out/'parent_validation.json',out/'parent_case.json']:
            rf['source_sha256'][p.relative_to(ROOT).as_posix()]=sha(p)
        put(relative+'/runtime/RUNTIME_FREEZE.json',rf)
        # Test the complete real contract using the current live clock before
        # starting physical simulation; source, route, nonce and role all bind.
        from .engine.contract import verify_root
        root=dict(version='G61_MIDFLIGHT_ROOT_V1',run_id=runid,arm='R18_MIDFLIGHT_EVENT',case=case,nonce=rf['nonces'][runid],epoch=rf['epoch'],
            runtime_freeze_sha256=sha(base/'runtime/RUNTIME_FREEZE.json'),owner_start=begin,owner_deadline=begin+120.)
        assert verify_root(root)
        for name,change in [('clock',lambda r:r.update(owner_start=r['owner_start']+.1,owner_deadline=r['owner_deadline']+.1)),
                            ('role',lambda r:r['case']['source_planned_route_identity'].update(current_live_initial_planning=False))]:
            bad=copy.deepcopy(root);change(bad)
            try:verify_root(bad)
            except ValueError:pass
            else:raise RuntimeError('LIVE_CONTRACT_NEGATIVE_FAILED:'+name)
        put(relative+'/LIVE_ROOT_ENTRY_TESTS.json',dict(status='PASS',checks=['positive_complete_live_root','reject_clock_reset','reject_wrong_input_role'],no_physical_motion_before_checks=True))
        stages['complete_initial_admission_and_freeze_s']=time.perf_counter()-begin
        if time.perf_counter()>=req['initial_planner_deadline_perf']:raise InitialOrdinaryFailure('INITIAL_COMPLETE_ADMISSION_LATE')
        from .engine import run
        flight=True;run.main(True,runid);summary=read(base/'runtime/summaries'/f'{runid}.json');status=summary['status'];fatal=summary['fatal']
    except BaseException as exc:
        from .checked_admission import InitialOrdinaryFailure
        if isinstance(exc,InitialOrdinaryFailure):status=str(exc);fatal=False
        else:
            status='STRONG_PIPELINE_STRUCTURAL_STOP';fatal=True
            put(relative+'/PIPELINE_EXCEPTION.json',dict(detail=type(exc).__name__+':'+str(exc),traceback=traceback.format_exc()))
    finally:
        if job is not None:job.close()
        wall=time.perf_counter()-begin
        if wall>=120. and status=='TASK_SUCCESS':status='OFFLINE_PIPELINE_WALL_TIMEOUT'
        put(relative+'/PIPELINE_RESULT.json',dict(status=status,strict_success=status=='TASK_SUCCESS',fatal=fatal,initial_planner_executed=True,physical_flight_started=flight,
            full_pipeline_inclusive_wall_s=wall,all_cold_initial_cost_paid=True,stages=stages,whole_wall_cap_s=120.,initial_worker_cap_s=20.,initial_planner_cap_s=60.,
            peak_initial_control=max(map(abs,tr['controls'])) if 'tr' in locals() else None,
            physical_time_s=summary['physical_time_s'] if summary else None,root_summary=summary,root_SEALED_sha256=summary['completion_sha256'] if summary else None))
        print('STRONG_LIVE_PIPELINE',status,wall,remaining(),flush=True)

if __name__=='__main__':main(float(sys.argv[1]),sys.argv[2])
