"""Request data scope and request deadline, independent of code/campaign location."""
import time
from pathlib import Path
from .io_core import ROOT as PROJECT_ROOT,CODE,read,sha,content,put as atomic_put,inside,native
from .layout import require_binding,assert_same
BOUND=require_binding()
ROOT=native(PROJECT_ROOT)
D=native(BOUND['campaign_dir'])
PREV=CODE

def elapsed():return time.perf_counter()-BOUND['paid_start_perf']
def remaining():return 120.-elapsed()
def check(reserve=0.):
    assert_same(BOUND)
    if reserve:raise ValueError('STUDY_RESERVE_IS_NOT_A_REQUEST_DEADLINE')
    if remaining()<=0:raise ValueError('REQUEST_ROOT_WALL_DEADLINE')
    return remaining()
def put(name,value):
    assert_same(BOUND);p=(D/name).resolve();inside(p,D);atomic_put(p,value,D)
def write(name,value):
    assert_same(BOUND);p=native(inside((D/name).resolve(),D));p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x',encoding='utf8',newline='\n') as stream:
        import os
        stream.write(value);stream.flush();os.fsync(stream.fileno())

