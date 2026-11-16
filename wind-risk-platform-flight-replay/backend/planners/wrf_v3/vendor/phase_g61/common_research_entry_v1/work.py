"""Common original16 research entry with explicit immutable mission roles."""
import hashlib,json,os,time
from pathlib import Path
D=Path(__file__).resolve().parent
ROOT=D.parents[1]
PREV=ROOT/'phase_g61/publication_race_coverage_v1'
SOURCE=ROOT/'phase_g61/native_gradient_validation_v1/source_v2/runtime'

def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):
    with Path(p).open('rb') as s:return hashlib.file_digest(s,'sha256').hexdigest()
def content(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def put(name,v):
    p=(D/name).resolve()
    if not p.is_relative_to(D):raise RuntimeError('RACE_ROUND_SCOPE')
    p.parent.mkdir(parents=True,exist_ok=True);b=json.dumps(v,sort_keys=True,indent=2,ensure_ascii=False,allow_nan=False).encode()
    with p.open('xb') as s:
        if s.write(b)!=len(b):raise RuntimeError('SHORT_WRITE')
        s.flush();os.fsync(s.fileno())
    if p.read_bytes()!=b:raise RuntimeError('READBACK')
def write(name,v):
    p=(D/name).resolve()
    if not p.is_relative_to(D):raise RuntimeError('RACE_ROUND_SCOPE')
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x',encoding='utf8',newline='\n') as s:s.write(v);s.flush();os.fsync(s.fileno())
def elapsed():return time.perf_counter()-read(D/'START.json')['start_perf']
def remaining():return read(D/'START.json')['cap_s']-elapsed()
def check(reserve=0.):
    r=remaining()
    if r<reserve:raise RuntimeError('ROUND_BUDGET')
    return r

if __name__=='__main__':
    old=read(PREV/'ROUND_CLOSE.json');start=read(PREV/'START.json');assert old['round_closed'] and old['continuous_overrun_s']==0
    put('START.json',dict(start_perf=start['start_perf']+old['continuous_elapsed_s'],cap_s=7200.,
        accounting='Continuous from preceding verified seal; intervening read-only diagnosis and design, all tests/tasks/history/package included',
        previous_goal_turn='PROGRESS: one realWRF postpublication invalidation task, independent quarantine and restart audit, sealed handoff and concrete unified-entry gap',
        previous_budget_closed_not_reopened=True,goal_status='ACTIVE',authorization='Full active five-part goal; prioritize untested causal feasibility, no repeat of passed conditions',
        scope='Common entry and role-bound contract for original16; structural/negative coverage all entries; at most one new short-route native-hour-crossing integration confirmation, no full16 rerun or optimizer tuning',
        original_8_20_60_120_and_physical_caps_unchanged=True,defaults_or_READY_upgrade=False))
    write('history.py',(PREV/'history.py').read_text(encoding='utf8'))
    print('NEW_COMMON_ENTRY_LEDGER',check(),flush=True)
