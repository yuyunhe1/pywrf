from pathlib import Path
import hashlib,json,os,tempfile
D=Path(__file__).resolve().parent
ROOT=D.parent.parent
PRIOR=ROOT/'phase4t/amendment_v1'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def encoded(v):return (v if isinstance(v,str) else json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)).encode('utf-8')
def put(p,v,*,replace=False):
    p=Path(p)
    if not p.is_absolute():p=D/p
    p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists() and not replace:raise FileExistsError(p)
    fd,tmp=tempfile.mkstemp(prefix='.'+p.name+'.',suffix='.tmp',dir=p.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(encoded(v));f.flush();os.fsync(f.fileno())
        if replace:os.replace(tmp,p)
        else:os.link(tmp,p);os.unlink(tmp)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
