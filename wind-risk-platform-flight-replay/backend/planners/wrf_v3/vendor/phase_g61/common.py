"""Phase G6.1 taskbook campaign bookkeeping; historical evidence is immutable."""
import concurrent.futures,hashlib,json,time
from pathlib import Path
D=Path(__file__).resolve().parent;ROOT=D.parent
CAP=7200.
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def content(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def put(rel,x):
    p=(D/rel).resolve()
    if not p.is_relative_to(D):raise RuntimeError('WRITE_OUTSIDE_PHASE_G61')
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x',encoding='utf-8',newline='\n') as f:json.dump(x,f,indent=2,sort_keys=True,ensure_ascii=False,allow_nan=False);f.write('\n')
def textfile(rel,x):
    p=(D/rel).resolve()
    if not p.is_relative_to(D):raise RuntimeError('WRITE_OUTSIDE_PHASE_G61')
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x',encoding='utf-8',newline='\n') as f:f.write(x)
def charge(stage,inclusive_s,**kw):
    with (D/'provenance/BUDGET_LEDGER.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(dict(stage=stage,inclusive_s=inclusive_s,**kw),ensure_ascii=False)+'\n')
def remaining():
    anchor=read(D/'provenance/START.json')['start_perf']
    amendment=D/'provenance/PRE_REGISTRATION_ACCOUNTING.json'
    if amendment.exists():anchor=read(amendment)['effective_start_perf']
    return CAP-(time.perf_counter()-anchor)
def history(after=False):
    if after:files=read(D/'provenance/HISTORY_BEFORE.json')['files']
    else:
        files=read(ROOT/'phase4t_g61/initial_plan_control_reserve_v1/provenance/HISTORY_AFTER.json')['files']
        protected=['phase4t','phase4t_g5','phase4t_g6','phase4t_g61','phase4m','phase4i','phase4s','phase3a_implementation','phase3c']
        for name in protected:
            for p in (ROOT/name).rglob('*'):
                if p.is_file():files.setdefault(p.relative_to(ROOT).as_posix(),sha(p))
    def check(item):
        rel,h=item;p=ROOT/rel;actual=sha(p) if p.is_file() else None
        return None if h==actual else dict(path=rel,expected=h,actual=actual)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:changed=[r for r in pool.map(check,files.items()) if r]
    put('provenance/HISTORY_'+('AFTER' if after else 'BEFORE')+'.json',dict(files=files,count=len(files),changes=changed))
    if changed:raise RuntimeError('HISTORY_HASH_CHANGED_STOP')
    return len(files)
