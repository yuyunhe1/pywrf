"""Own-directory atomic I/O, audit clocks, and cumulative execution accounting."""
from pathlib import Path
import datetime
import hashlib
import json
import os
import time

from phase4a_design.mock_contract import content_hash
from phase4t.amendment_v1.model import StructuralStop, WallLimit

D = Path(__file__).resolve().parent
ROOT = D.parents[2]
DESIGN = D.parent

def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as s:
        for block in iter(lambda:s.read(2**20),b''):h.update(block)
    return h.hexdigest()

def own(path):
    p=Path(path).resolve()
    if not p.is_relative_to(D):raise StructuralStop('WRITE_OUTSIDE_NEW_IMPLEMENTATION_DIRECTORY')
    return p

def write_all(fd,data,write=os.write):
    view=memoryview(data)
    while view:
        n=write(fd,view)
        if n is None or n<=0 or n>len(view):raise StructuralStop('ATOMIC_WRITE_STALLED_OR_INVALID')
        view=view[n:]

def put(path,obj,*,replace=False,write=os.write,fsync=os.fsync):
    p=own(path);p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists() and not replace:raise StructuralStop('REFUSE_EVIDENCE_OVERWRITE:'+str(p))
    tmp=p.with_name(p.name+'.tmp')
    stamp=time.perf_counter();data=(json.dumps(obj,sort_keys=True,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8');_trace.record('json_encode',stamp,path=str(path),bytes=len(data))
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o666)
    try:
        stamp=time.perf_counter();write_all(fd,data,write);_trace.record('write_all',stamp,path=str(path))
        stamp=time.perf_counter();fsync(fd);_trace.record('fsync',stamp,path=str(path))
    finally:os.close(fd)
    stamp=time.perf_counter();os.replace(tmp,p);_trace.record('atomic_replace',stamp,path=str(path))
    return sha(p)

def textfile(path,text):
    p=own(path);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x',encoding='utf-8',newline='\n') as s:s.write(text)

def event(path,name,**kw):
    p=own(path);p.parent.mkdir(parents=True,exist_ok=True)
    row=dict(event=name,perf_s=time.perf_counter(),utc=utc(),**kw)
    data=(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o666)
    try:write_all(fd,data);os.fsync(fd)
    finally:os.close(fd)
    return row

def charge(stage,elapsed,category='PREFLIGHT',**kw):
    return event(D/'BUDGET.jsonl',stage,inclusive_wall_s=elapsed,category=category,**kw)

def spent(category=None):
    p=D/'BUDGET.jsonl'
    rows=[__import__('json').loads(l) for l in p.read_text(encoding='utf-8').splitlines()] if p.exists() else []
    return sum(r.get('inclusive_wall_s',0.) for r in rows if r.get('additive',True) and (category is None or r.get('category')!='FORMAL_OWNER_INCLUSIVE'))


class Deadline:
    def __init__(self,absolute,label,now=time.perf_counter):self.absolute=absolute;self.label=label;self.now=now
    def check(self):
        if self.now()>=self.absolute:raise WallLimit(self.label)

def packet(kind,request,**payload):
    body=dict(version='G5_TYPED_PACKET_V1',kind=kind,run_id=request['run_id'],nonce=request['nonce'],
        task_epoch=request['task_epoch'],request_sha256=content_hash(request),
        context_id=request['mission']['original_context_id'],**payload)
    return dict(payload=body,payload_sha256=content_hash(body))

def unpack(value,kind,request):
    if not isinstance(value,dict) or set(value)!= {'payload','payload_sha256'}:raise StructuralStop('IPC_INVALID_PACKET_SCHEMA')
    body=value['payload']
    if not isinstance(body,dict) or content_hash(body)!=value['payload_sha256']:raise StructuralStop('IPC_INVALID_PACKET_HASH')
    trusted=dict(version='G5_TYPED_PACKET_V1',kind=kind,run_id=request['run_id'],nonce=request['nonce'],
        task_epoch=request['task_epoch'],request_sha256=content_hash(request),context_id=request['mission']['original_context_id'])
    if any(body.get(k)!=v for k,v in trusted.items()):raise StructuralStop('IPC_INVALID_REQUEST_EPOCH_OR_CONTEXT')
    return body


# Trace records are buffered; original atomic fsync and own-directory guard remain.
from . import trace as _trace
_read=read
def read(path):
    start=time.perf_counter();value=Path(path).read_text(encoding='utf-8-sig')
    _trace.record('read_file',start,path=str(path),bytes=len(value))
    start=time.perf_counter();out=json.loads(value);_trace.record('json_decode',start,path=str(path));return out
_put=put
def put(path,obj,**kwargs):
    with _trace.span('atomic_put_total',path=str(path)):
        return _put(path,obj,**kwargs)
_sha=sha
def sha(path):
    with _trace.span('sha256_file',path=str(path)):return _sha(path)
