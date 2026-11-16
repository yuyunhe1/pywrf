"""Finite first-batch I/O, identities and unchanged deadlines."""
import copy,hashlib,json,math,os,tempfile,time
from pathlib import Path
from ...io_core import ROOT,read,sha,content
from ...work import D as CAMPAIGN,BOUND
from ...layout import assert_same
_case_key=os.environ.get('G61_CAUSAL_ENV_CASE','')
from ...registry import checked_registry
if _case_key not in checked_registry()['entries']:raise RuntimeError('UNREGISTERED_RUNTIME_CASE')

D=CAMPAIGN/'tasks'/_case_key/'runtime'

class Stop(Exception):pass
class Limit(Exception):pass

def put(path,value,replace=False):
    assert_same(BOUND)
    from ...io_core import put as atomic_put
    p=Path(path);p=p if p.is_absolute() else D/p
    try:return atomic_put(p,value,CAMPAIGN,replace=replace)
    except ValueError as exc:raise Stop(str(exc)) from exc


def packet(req,kind,payload):
    body=dict(kind=kind,request_sha256=content(req),context_input_sha256=content(req['case']),
              nonce=req['nonce'],run_id=req['run_id'],payload=payload)
    return dict(body=body,sha256=content(body))

def unpack(req,kind,value):
    if not isinstance(value,dict) or set(value)!=set(('body','sha256')) or not isinstance(value.get('body'),dict):raise Stop('IPC_SCHEMA')
    b=value['body']
    if set(b)!=set(('kind','request_sha256','context_input_sha256','nonce','run_id','payload')) or not isinstance(b.get('payload'),dict):raise Stop('IPC_SCHEMA')
    if value['sha256']!=content(b) or b['kind']!=kind or b['request_sha256']!=content(req) or b['context_input_sha256']!=content(req['case']) or b['nonce']!=req['nonce'] or b['run_id']!=req['run_id']:
        raise Stop('IPC_IDENTITY_OR_HASH')
    return b['payload']

class Clock:
    def __init__(self,deadline):
        if isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline):raise Stop('CLOCK_SCHEMA')
        self.deadline=deadline;self._expected_deadline=deadline
    def check(self):
        if self.deadline!=self._expected_deadline:raise Stop('CLOCK_MUTATED')
        if time.perf_counter()>=self.deadline:raise Limit('WALL_DEADLINE')

class Timed:
    def __init__(self,provider,clock):
        self.original=provider;self.clock=clock;self._identity=(id(provider),id(clock));self._sealed=True
    def __setattr__(self,name,value):
        if self.__dict__.get('_sealed'):raise Stop('TIMED_WRAPPER_IMMUTABLE')
        object.__setattr__(self,name,value)
    def __getattr__(self,name):return getattr(self.original,name)
    def wind_query(self,*args):
        if (id(self.original),id(self.clock))!=self._identity:raise Stop('TIMED_WRAPPER_CHANGED')
        self.clock.check()
        from phase3a_implementation.adapter import AdapterError
        try:r=self.original.wind_query(*args)
        except AdapterError as e:raise Stop('PROVIDER_BINDING_ERROR:'+str(e)) from e
        self.clock.check();return r

def verify(req):
    try:
        from ..contract import verify_transaction
        return verify_transaction(req)
    except ValueError as exc:raise Stop(str(exc)) from exc

def control_ok(u,d):
    if isinstance(u,bool) or isinstance(d,bool) or not isinstance(u,(int,float)) or not isinstance(d,(int,float)) or not math.isfinite(u) or not math.isfinite(d) or d<=0 or abs(u)>10/8.513422179807296+1e-12:
        raise Stop('ACTUAL_PREAPPLY_CONTROL_OR_DURATION')
    return True

def result_schema(value,kind):
    if kind not in ('SOLVER_RESULT','OWNER_RESULT'):raise Stop('WORKER_KIND_SCHEMA')
    if not isinstance(value,dict) or not isinstance(value.get('status'),str):raise Stop('WORKER_SCHEMA_INVALID')
    if kind=='SOLVER_RESULT':
        if value['status'] not in ('NEW_ROUTE_FOUND','REPLAN_NO_VALID_ROUTE','REPLAN_BUDGET_EXCEEDED','STRUCTURAL_STOP','REPLAN_DOMAIN_REJECT','REPLAN_COLLISION_REJECT','DYNAMIC_WIND_QUERY_ERROR'):raise Stop('SOLVER_STATUS_SCHEMA')
        if (value['status']=='NEW_ROUTE_FOUND')!=isinstance(value.get('result'),dict):raise Stop('SOLVER_RESULT_SCHEMA')
        if isinstance(value.get('published_perf'),bool) or not isinstance(value.get('published_perf'),(int,float)) or not math.isfinite(value['published_perf']):raise Stop('SOLVER_CLOCK_SCHEMA')
    else:
        known=('TASK_SUCCESS','REPLAN_NO_VALID_ROUTE','REPLAN_BUDGET_EXCEEDED','STRUCTURAL_STOP','REPLAN_DOMAIN_REJECT','REPLAN_COLLISION_REJECT','DYNAMIC_WIND_QUERY_ERROR','REPLAN_PHYSICAL_HORIZON_EXHAUSTED','EXECUTION_NUMERICAL_FAIL_ZERO_DURATION_REFERENCE','REPLAN_HEADING_MISMATCH','EXECUTION_TERMINAL_OR_ORDERED_FAIL','OFFLINE_PIPELINE_WALL_TIMEOUT','EXECUTION_DOMAIN_FAIL','EXECUTION_NUMERICAL_FAIL')
        known+=('CAUSAL_ATTEMPT_LIMIT_EXHAUSTED','EXECUTION_COLLISION','EXECUTION_PHYSICAL_TIMEOUT',
                'LOCAL_RECOVERY_UNSUPPORTED_WITHOUT_AUTHORIZED_TRIGGER','NO_MIDFLIGHT_REPLAN_WAS_EXERCISED',
                'NO_NORMAL_OBSERVATION_BEFORE_ORIGINAL_HANDOVER','PREPARATION_GROUP_LIMIT_EXHAUSTED',
                'PREPARED_ACK_LATE_OR_REFUSED','INITIAL8S_WINDOW_INSUFFICIENT','ORIGINAL_PREPARATION_WINDOW_EXHAUSTED')
        if value['status'] not in known:raise Stop('OWNER_STATUS_SCHEMA')
        if type(value.get('strict_success')) is not bool or type(value.get('fatal')) is not bool:raise Stop('OWNER_RESULT_BOOL_SCHEMA')
        if value['strict_success']!=(value['status']=='TASK_SUCCESS') or (value['strict_success'] and value['fatal']):raise Stop('OWNER_FALSE_SUCCESS_SCHEMA')
        if value['fatal']!=(value['status']=='STRUCTURAL_STOP'):raise Stop('OWNER_FATAL_STATUS_SCHEMA')
    return True

def source_check():
    assert_same(BOUND)
    freeze=read(D/'RUNTIME_FREEZE.json')
    active=freeze.get('active_source_sha256')
    if not isinstance(active,dict) or not active:raise Stop('ACTIVE_CLOSURE_REQUIRED')
    for p,h in active.items():
        if sha(ROOT/p)!=h:raise Stop('SOURCE_CHANGED:'+p)
    return True
