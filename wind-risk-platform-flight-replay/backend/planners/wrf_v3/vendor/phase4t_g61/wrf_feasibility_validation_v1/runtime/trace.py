"""Buffered timing: no per-span disk writes on the preparation critical path."""
import contextlib,os,time
ROWS=[]
def record(name,start,end=None,**detail):
    ROWS.append(dict(name=name,start_perf=start,end_perf=time.perf_counter() if end is None else end,pid=os.getpid(),**detail))
@contextlib.contextmanager
def span(name,**detail):
    t=time.perf_counter();status='RETURNED'
    try:yield
    except BaseException as exc:status=type(exc).__name__;raise
    finally:record(name,t,status=status,**detail)
def timed(name,fn,*a,**kw):
    with span(name):return fn(*a,**kw)
def flush(base,role):
    from .common import put
    rows=list(ROWS)
    put(base/('trace_'+role+'.json'),dict(role=role,pid=os.getpid(),rows=rows,note='inclusive nested spans; phase spans may overlap I/O/ODE/child; flush itself outside snapshot, included in process wall'))
