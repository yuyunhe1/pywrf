"""Per-process diagnostics, no historical output paths."""
EVENTS=[]
def record(kind,start,**kw):
    import time
    EVENTS.append(dict(kind=kind,start_perf=start,observed_perf=time.perf_counter(),**kw))
