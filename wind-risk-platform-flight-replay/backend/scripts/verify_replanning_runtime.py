"""Repeated real search/cancel stress; isolated jobs, no route history writes."""
import argparse
import gc
import json
import os
from pathlib import Path
import sys
from time import monotonic,sleep
import weakref

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
sys.path.insert(0,str(BACKEND))
sys.stdout.reconfigure(encoding='utf-8')
os.environ.update(GFS_AUTO_DOWNLOAD='0',WRF_CACHE_AUTO_SYNC_INDEX='0',WRF_CACHE_REMOTE_HOST='',
    ROUTE_PLAN_DIRECTION_PROCESSES='2',ROUTE_PLAN_WEATHER_LOADING='full',
    ROUTE_PLAN_MAX_TEMPORAL_LABELS='0',ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='0')
from app.models import RecommendationJobRequest
from app.recommendations import RecommendationJobManager
from app.planning_clients import PlanningClients
from app.parallel_routing import ParallelPlanner
from app.direction_pool import shutdown_direction_pool
from app.wrf_timeline import load_dataset
from datetime import datetime

WORK=ROOT/'data/replanning_cache_tests'


def memory_mb():
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD),*[(name,ctypes.c_size_t) for name in
            ('peak','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile')]]
    value=Counters();value.cb=ctypes.sizeof(value)
    current=ctypes.windll.kernel32.GetCurrentProcess
    current.restype=wintypes.HANDLE
    get=ctypes.windll.psapi.GetProcessMemoryInfo
    get.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    assert get(current(),ctypes.byref(value),value.cb)
    return round(value.working/1024**2,2)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--phase',required=True)
    parser.add_argument('--repeats',type=int,default=4)
    parser.add_argument('--seconds',type=float,default=2)
    parser.add_argument('--sequences',action='store_true')
    parser.add_argument('--gc-enabled',action='store_true')
    args=parser.parse_args()
    WORK.mkdir(parents=True,exist_ok=True)
    req=RecommendationJobRequest(start=[118.83362,30.39657],end=[119.05334,30.15938],source='wrf',
        cycle='2026-11-10 06:00 UTC',level='100m AGL',departure_at='2026-11-10T15:16:00Z',
        average_ground_speed_mps=10,client_instance_id='isolated-replan-page')
    data=load_dataset(req.level,req.departure_at,req.cycle)
    assert data.frames
    references=[];rows=[]
    original=ParallelPlanner.__init__
    def track(self,*a,**kw):
        original(self,*a,**kw);references.append(weakref.ref(self))
    ParallelPlanner.__init__=track
    manager=RecommendationJobManager(workers=1,timeout_seconds=30,owner_timeout_seconds=60)
    pages=PlanningClients(manager)
    # Deterministic ownership test: cleanup must not depend on the next cyclic
    # GC pass. A final GC-only measurement distinguishes cycles from live work.
    gc.collect()
    if not args.gc_enabled: gc.disable()
    initial=memory_mb()
    try:
        for index in range(args.repeats):
            current=req.model_copy(update={'planning_strategy':['wind_avoidance','distance_priority','balanced'][index%3],
                'client_request_sequence':index+1 if args.sequences else None})
            state=pages.submit(pages.begin(current),manager,lambda:manager.submit_workflow(current,data))
            job=manager.jobs[state['job_id']]
            sleep(args.seconds)
            began=monotonic()
            if args.sequences: pages.cancel(current.client_instance_id,index+1,forget=True)
            else: manager.cancel_job(job.job_id)
            while not job.future.done():
                assert monotonic()-began<5,'cancelled computation did not exit'
                sleep(.01)
            sleep(.1)
            row=dict(iteration=index+1,status=job.status,release_seconds=monotonic()-began,
                live_search_objects=sum(ref() is not None for ref in references),working_set_mb=memory_mb(),
                retained_results=sum(j.result is not None or bool(getattr(j,'workflow',None)) for j in manager.jobs.values()))
            rows.append(row);print(json.dumps(row),flush=True)
        before_gc=memory_mb();gc.enable();gc.collect()
        report=dict(phase=args.phase,writes_history=False,gc_disabled_during_search=not args.gc_enabled,
            page_sequences=args.sequences,initial_mb=initial,
            rows=rows,before_gc_mb=before_gc,after_gc_mb=memory_mb(),
            live_after_gc=sum(ref() is not None for ref in references))
        (WORK/f'{args.phase}-runtime.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    finally:
        gc.enable();manager.shutdown();shutdown_direction_pool()
        ParallelPlanner.__init__=original


if __name__=='__main__':main()
