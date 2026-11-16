"""Production source template for a same-worker one-use recovery C origin.

Copied into a new frozen runtime by prepare_c_reuse.py, never into old results.
"""
import copy,inspect,math,time,uuid
from pathlib import Path
import scipy.integrate
import phase3b.validator as validator_module
from . import connector,environment
from .common import D,ROOT,Stop,read,sha,content,put,packet,unpack


class RecoveryCOrigin:
    def __init__(self,req,ctx,start,goal,leg,base):
        self.req=req;self.ctx=ctx;self.start=copy.deepcopy(start);self.goal=copy.deepcopy(goal);self.leg=leg;self.base=Path(base)
        self.instance=uuid.uuid4().hex;self.pending=None;self.last_event=None
        self.freeze=read(D/'RUNTIME_FREEZE.json')
        if sha(D/'RUNTIME_FREEZE.json')!=req['runtime_freeze_sha256']:raise Stop('C_ORIGIN_RUNTIME_FREEZE')
        self.expected_case=content(req['case']);self.expected_request=content(req)

    def bind(self,tr,total,index,stage):
        ctx=self.ctx;ctx.assert_bound();ctx.clock.check()
        if stage!='ENDPOINT_RECOVERY' or type(index) is not int or index<0:raise Stop('C_ORIGIN_STAGE')
        if content(self.req)!=self.expected_request or content(self.req['case'])!=self.expected_case:raise Stop('C_ORIGIN_REQUEST_MUTATED')
        if ctx.descriptor['case_sha256']!=self.expected_case or ctx.id!=content(ctx.descriptor):raise Stop('C_ORIGIN_CONTEXT')
        if tr['state_start']!=self.start or tr['state_goal']!=self.goal:raise Stop('C_ORIGIN_BOUNDARY')
        if ctx.clock.deadline!=self.req['prepare_deadline']:raise Stop('C_ORIGIN_CLOCK')
        if not math.isfinite(total) or total<0:raise Stop('C_ORIGIN_TIME')
        if connector.inherited_backend.validate is not validator_module.validate or environment.validate is not validator_module.validate:
            raise Stop('C_ORIGIN_CALLBACK_CHAIN')
        fn=validator_module.validate
        if fn.__module__!='phase3b.validator' or fn.__name__!='validate' or fn.__kwdefaults__!={'max_step':.1}:
            raise Stop('C_ORIGIN_NUMERICAL_DEFAULTS')
        if validator_module.solve_ivp is not scipy.integrate.solve_ivp or validator_module.THRESHOLDS!=ctx.descriptor['C']['thresholds']:
            raise Stop('C_ORIGIN_BACKEND_OR_THRESHOLDS')
        paths=[Path(inspect.getsourcefile(fn)),Path(connector.__file__),Path(environment.__file__),Path(__file__)]
        for p in paths:
            rel=p.relative_to(ROOT).as_posix()
            if self.freeze['source_sha256'].get(rel)!=sha(p):raise Stop('C_ORIGIN_SOURCE_CHANGED')
        external=self.freeze['external_numerical_source_sha256']
        backend=Path(inspect.getsourcefile(validator_module.solve_ivp))
        if external.get(str(backend))!=sha(backend):raise Stop('C_ORIGIN_EXTERNAL_BACKEND_SOURCE')
        binding=dict(version='PROSPECTIVE_RECOVERY_C_ORIGIN_V1',instance=self.instance,request_sha256=self.expected_request,
            case_sha256=self.expected_case,context_id=ctx.id,context_descriptor_sha256=content(ctx.descriptor),
            provider_object=id(ctx.provider),clock_object=id(ctx.clock),deadline=float(ctx.clock.deadline).hex(),
            source_sha256=content(ctx.provider.source),physical_offset_s=ctx.offset,trajectory_sha256=content(tr),
            controls_sha256=content(tr['controls']),total_float_hex=float(total).hex(),stage=stage,seed_index=index,leg=self.leg,
            actual_C_callable=id(fn),actual_C_code=id(fn.__code__),actual_backend_callable=id(validator_module.solve_ivp),
            callable_defaults=copy.deepcopy(fn.__kwdefaults__),thresholds=copy.deepcopy(validator_module.THRESHOLDS),
            max_step=.1,method='INDEPENDENT_DOP853',rtol=1e-11,atol=[8.513422179807296e-12,8.513422179807296e-12,1e-12])
        ctx.clock.check();return binding

    def produce(self,tr,total,index,stage):
        if self.pending is not None:raise Stop('C_ORIGIN_UNCONSUMED')
        before=self.bind(tr,total,index,stage);start=time.perf_counter()
        # Exact original independent C callback and arguments. No new model,
        # tolerance, integration mesh, candidate controls, or solver parameter.
        result=connector.validate(tr,tr['controls'],total,self.ctx.provider)
        finished=time.perf_counter();after=self.bind(tr,total,index,stage)
        if before!=after:raise Stop('C_ORIGIN_INPUT_CHANGED_DURING_C')
        if result['valid']:
            if result['failures'] or result['domain_status']!='IN_DOMAIN_AT_EVALUATED_POINTS' or result['thresholds']!=before['thresholds']:
                raise Stop('C_ORIGIN_INVALID_RECEIPT')
            origin=dict(binding=before,C_sha256=content(result),C=copy.deepcopy(result),
                C_started_perf=start,C_finished_perf=finished,actual_C_wall_s=finished-start,
                actual_C_evaluations=1,actual_C_wind_query_count=result['query_count'],subsequent_reuse_query_count=0)
            path=self.base/'C_origins'/f'{index:03d}_{stage}.json'
            proof=packet(self.req,'WORKER_RECOVERY_C_ORIGIN',origin);put(path,proof)
            self.ctx.clock.check()
            self.pending=dict(result_object=result,binding=before,C_sha256=origin['C_sha256'],path=path,packet_sha256=proof['sha256'])
        return result

    def consume(self,result,tr,total,index,stage):
        start=time.perf_counter();pending=self.pending
        if pending is None or result is not pending['result_object']:raise Stop('C_ORIGIN_MISSING_OR_FOREIGN')
        live=self.bind(tr,total,index,stage)
        if live!=pending['binding'] or content(result)!=pending['C_sha256']:raise Stop('C_ORIGIN_BINDING_OR_BODY')
        saved=read(pending['path']);origin=unpack(self.req,'WORKER_RECOVERY_C_ORIGIN',saved)
        if saved['sha256']!=pending['packet_sha256'] or origin['binding']!=live or origin['C_sha256']!=content(origin['C']) or origin['C']!=result:
            raise Stop('C_ORIGIN_DURABLE_READBACK')
        answer=copy.deepcopy(origin['C']);self.ctx.assert_bound();self.ctx.clock.check()
        self.last_event=dict(origin_file=str(pending['path']),origin_packet_sha256=saved['sha256'],
            producer_C_s=origin['actual_C_wall_s'],producer_C_query_count=origin['actual_C_wind_query_count'],
            reused=True,fresh_candidate_C_evaluations=0,fresh_candidate_C_wind_queries=0,
            handoff_checks_s=time.perf_counter()-start,C_started_perf=origin['C_started_perf'],C_finished_perf=origin['C_finished_perf'])
        self.pending=None
        return answer
