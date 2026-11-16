"""Cold single-use original mission request with owned-process cleanup."""
import os,subprocess,sys,time


def main(case_key,paid_start=None):
    observed=time.perf_counter()
    if paid_start is not None and (isinstance(paid_start,bool) or not isinstance(paid_start,(int,float)) or not 0<paid_start<=observed):raise ValueError('ENTRY_PAID_START_CLOCK')
    from .work import BOUND
    if paid_start is not None and float(paid_start)!=BOUND['paid_start_perf']:raise ValueError('REQUEST_LAUNCH_CLOCK_RESET')
    begin=BOUND['paid_start_perf']
    from .work import D,ROOT,read,put,check,remaining
    from phase4o.ownership import OwnedJob
    check();gate=read(D/'EXECUTION_GATE.json');assert gate['status']=='PASS'
    assert case_key in gate['order']
    index=gate['order'].index(case_key)
    for prior in gate['order'][:index]:
        receipt=read(D/'tasks'/prior/'OUTER_RECEIPT.json')
        if receipt['fatal']:raise RuntimeError('PRIOR_STRUCTURAL_STOP_NO_BATCH_EXPANSION')
    base=D/'tasks'/case_key
    if (base/'PIPELINE_REQUEST.json').exists():raise RuntimeError('REFUSE_CAUSAL_TASK_RETRY')
    with (base/'pipeline_stdout.txt').open('x') as out,(base/'pipeline_stderr.txt').open('x') as err:
        process=subprocess.Popen([sys.executable,'-X','utf8','-B','-m','phase_g61.request_runtime_confirmation_v1.runtime_v3.pipeline_v4',str(begin),case_key],
            cwd=ROOT,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',G61_CAUSAL_ENV_CASE=case_key),stdout=out,stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    job=OwnedJob(process)
    try:process.wait(timeout=max(.001,begin+120.-time.perf_counter()))
    except subprocess.TimeoutExpired:pass
    finally:job.close()
    wall=time.perf_counter()-begin
    result=read(base/'PIPELINE_RESULT.json') if (base/'PIPELINE_RESULT.json').exists() else dict(
        status='LIVE_PIPELINE_PROCESS_TIMEOUT_OR_CRASH',strict_success=False,fatal=process.returncode not in (None,0) and wall<120.)
    put('tasks/'+case_key+'/OUTER_RECEIPT.json',dict(status=result['status'] if wall<120. else 'OFFLINE_PIPELINE_WALL_TIMEOUT',
        strict_success=result['strict_success'] and wall<120.,root_pipeline=result,exitcode=process.returncode,
        outer_start_perf=begin,after_cleanup_wall_s=wall,cap_s=120.,workers_reaped=True,case_key=case_key,fatal=result.get('fatal',False)))
    print('CAUSAL_FULL_TASK',case_key,result['status'],wall,remaining(),flush=True)


if __name__=='__main__':main(sys.argv[1])
