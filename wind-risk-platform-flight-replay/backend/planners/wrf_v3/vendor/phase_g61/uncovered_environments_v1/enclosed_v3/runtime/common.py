"""Finite first-batch I/O, identities and unchanged deadlines."""
import copy,hashlib,json,math,os,tempfile,time
from pathlib import Path
from phase_g61.common import ROOT,D as CAMPAIGN,read,sha,content
D=CAMPAIGN/'uncovered_environments_v1'/'enclosed_v3'/'runtime'

class Stop(Exception):pass
class Limit(Exception):pass

def put(path,value,replace=False):
    p=Path(path);p=p.resolve() if p.is_absolute() else (D/p).resolve()
    if not p.is_relative_to(CAMPAIGN):raise Stop('WRITE_OUTSIDE_NEW_CAMPAIGN')
    p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists() and not replace:raise Stop('REFUSE_OVERWRITE:'+str(p))
    fd,tmp=tempfile.mkstemp(prefix='.'+p.name+'.',dir=p.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            body=json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
            written=f.write(body)
            if written!=len(body):raise Stop('SHORT_WRITE')
            f.flush();os.fsync(f.fileno())
        if replace:os.replace(tmp,p)
        else:os.link(tmp,p);os.unlink(tmp)
        if p.read_bytes()!=body:raise Stop('FULL_BODY_READBACK_MISMATCH')
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

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
    if not isinstance(req,dict) or req.get('version')!='G61_ENCLOSED_FLIGHT_V3':raise Stop('REQUEST_SCHEMA')
    if req.get('arm') not in ('R0_LEGACY_FIRST_VALID','R1_LIFT_AWARE','R2_LIFT_CONSTRAINED_RECOVERY','R3_REWOUND_LIFT_RECOVERY','R4_TWO_SEGMENT_RECOVERY','R5_COMPENSATED_ORDER','R7_UNCOVERED_ENVIRONMENTS','R8_REFERENCE_SEEDED','R9_ENCLOSED_FLIGHT'):raise Stop('ARM_SCHEMA')
    if not isinstance(req.get('nonce'),str) or len(req['nonce'])<16:raise Stop('NONCE_SCHEMA')
    if any(isinstance(req.get(k),bool) or not isinstance(req.get(k),(int,float)) or not math.isfinite(req[k]) for k in ('owner_start','owner_deadline','prepare_deadline')):
        raise Stop('CLOCK_SCHEMA')
    freeze=read(D/'RUNTIME_FREEZE.json')
    if sha(D/'RUNTIME_FREEZE.json')!=req['runtime_freeze_sha256'] or freeze['epoch']!=req['epoch']:
        raise Stop('RUNTIME_FREEZE_IDENTITY')
    manifest=read(CAMPAIGN/'uncovered_environments_v1'/'enclosed_v3'/'EXECUTION_MANIFEST.json')
    if sha(CAMPAIGN/'uncovered_environments_v1'/'enclosed_v3'/'EXECUTION_MANIFEST.json')!=freeze['manifest_sha256']:
        raise Stop('MANIFEST_IDENTITY')
    if not any(content(req['case'])==content(c) for c in manifest['cases']) or not any(r['run_id']==req['run_id'] and r['arm']==req['arm'] and r['case_id']==req['case']['id'] for r in manifest['order']):
        raise Stop('CASE_ARM_IDENTITY')
    if freeze['nonces'].get(req['run_id'])!=req['nonce']:raise Stop('NONCE_IDENTITY')
    if req['prepare_deadline']!=req['owner_start']+8. or req['owner_deadline']!=req['owner_start']+120.:
        raise Stop('BUDGET_CHANGED')
    return True

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
        if value['status'] not in known:raise Stop('OWNER_STATUS_SCHEMA')
        if type(value.get('strict_success')) is not bool or type(value.get('fatal')) is not bool:raise Stop('OWNER_RESULT_BOOL_SCHEMA')
        if value['strict_success'] and (value['status']!='TASK_SUCCESS' or value['fatal']):raise Stop('OWNER_FALSE_SUCCESS_SCHEMA')
    return True

def source_check():
    freeze=read(D/'RUNTIME_FREEZE.json')
    for p,h in freeze['source_sha256'].items():
        if sha(ROOT/p)!=h:raise Stop('SOURCE_CHANGED:'+p)
    return True
