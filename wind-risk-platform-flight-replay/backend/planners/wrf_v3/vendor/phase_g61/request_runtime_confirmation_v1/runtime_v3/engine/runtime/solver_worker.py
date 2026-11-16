import sys,time,traceback,math
from pathlib import Path
from unittest.mock import patch
from .common import read,put,packet,unpack,verify,Clock,Limit,Stop,content

def main(request_path,base_path):
    base=Path(base_path);req=read(request_path);stages={};begin=time.perf_counter();out=None;phase='SOLVER_REQUEST';phase_begin=begin
    try:
        Clock(req['prepare_deadline']).check()
        verify(req)
        from .common import source_check
        source_check()
        phase='SOLVER_DEPENDENCY_IMPORTS';phase_begin=time.perf_counter()
        from .environment import load,Context
        from .selection import solve
        stages['solver_dependency_imports_s']=time.perf_counter()-phase_begin
        clock=Clock(min(req['prepare_deadline'],begin+20.))
        phase='SOLVER_CONTEXT';phase_begin=time.perf_counter()
        t=time.perf_counter();raw=load(req['case']);ctx=Context(req['case'],raw,clock,req['prefix_duration_s']);stages['solver_context_s']=time.perf_counter()-t
        while not (base/'solver_input.json').exists():
            clock.check();time.sleep(.002)
        data=unpack(req,'SOLVER_INPUT',read(base/'solver_input.json'));start=data['predicted_handover_state']
        if data['local_handover_s']!=req['prefix_duration_s'] or data['physical_handover_s']!=req['physical_handover_s']:raise Stop('CAUSAL_SOLVER_CLOCK_BINDING')
        from phase2b4.solver_a.general_wind_connector import GeneralWindConnector
        original=GeneralWindConnector.connect;pool_index=0
        def captured(self,*args,**kw):
            nonlocal pool_index
            clock.check();result=original(self,*args,**kw);clock.check()
            put(base/'ordered_pools'/f'{pool_index:03d}.json',dict(request_sha256=content(req),
                args=[v.tolist() if hasattr(v,'tolist') else v for v in args],kwargs=kw,pool=result,pool_sha256=content(result.get('candidates',[]))))
            pool_index+=1
            if result.get('status')=='INVALID_INPUT' and len(args)>=4 and args[3]=={'Va':10.,'R0':8.513422179807296}:
                vector=[args[2]['wx'],args[2]['wy']] if isinstance(args[2],dict) else args[2]
                if math.hypot(*vector)<10.:raise Stop('INVALID_INTERNAL_SEED_REQUEST:'+str(result.get('error')))
            return result
        phase='ORDERED_BVP_CANDIDATES_WORKER_C_COLLISION';phase_begin=time.perf_counter();t=time.perf_counter()
        with patch.object(GeneralWindConnector,'connect',captured):out=solve(req,ctx,start,base/'solver')
        stages['candidate_generation_validation_inclusive_s']=time.perf_counter()-t
        clock.check()
        payload=dict(status='NEW_ROUTE_FOUND' if out else 'REPLAN_NO_VALID_ROUTE',result=out,stages=stages,
            pools=pool_index,published_perf=time.perf_counter(),solver_inclusive_s=time.perf_counter()-begin,
            context_id=ctx.id,actual_time_origin_s=math.fsum((req['case']['global_physical_offset_s'],req['prefix_duration_s'])),local_role_offset_s=req['prefix_duration_s'])
    except Limit as e:
        payload=dict(status='REPLAN_BUDGET_EXCEEDED',result=None,detail=str(e),stages=stages,published_perf=time.perf_counter(),interrupted_phase=phase,interrupted_phase_inclusive_s=time.perf_counter()-phase_begin)
    except BaseException as e:
        payload=dict(status='STRUCTURAL_STOP',result=None,detail=type(e).__name__+': '+str(e),traceback=traceback.format_exc(),stages=stages,published_perf=time.perf_counter())
    try:
        if 'raw' in locals() and hasattr(raw,'query_stats'):
            stats=raw.query_stats();put(base/'solver_query_stats.json',packet(req,'SOLVER_QUERY_STATS',stats))
            counts={k:v for k,v in stats['counts'].items() if k not in ('queries','OK')}
            payload['query_failure_counts']=counts
            if payload['status']=='REPLAN_NO_VALID_ROUTE':
                if any(k in counts for k in ('OUT_OF_TIME_DOMAIN','TIME_GAP_EXCEEDS_CONTRACT','INVALID_INPUT')):payload['status']='DYNAMIC_WIND_QUERY_ERROR'
                elif counts:payload['status']='REPLAN_DOMAIN_REJECT'
    finally:
        payload['published_perf']=time.perf_counter()
        put(base/'solver_result.json',packet(req,'SOLVER_RESULT',payload))
        persisted=time.perf_counter()
        from .common import sha
        put(base/'solver_publication.json',packet(req,'SOLVER_PUBLICATION',dict(solver_result_file_sha256=sha(base/'solver_result.json'),
            persisted_readback_perf=persisted,prepare_deadline=req['prepare_deadline'],timely=persisted<req['prepare_deadline'])))
    if payload['status']=='NEW_ROUTE_FOUND':
        from ...parent_handoff import produce
        produce(req,raw,payload['result'],base,clock)

if __name__=='__main__':main(*sys.argv[1:])
