"""Real V3 subprocess timeout retaining first solution and starting windows."""
import json
import os
from pathlib import Path
import sys
from time import monotonic,sleep

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
sys.path.insert(0,str(BACKEND))
sys.stdout.reconfigure(encoding='utf-8')
from app import v3_native,v3_service
from app.models import RoutePlanRequest
from app.route_jobs import RouteJobManager,TERMINAL

request=RoutePlanRequest(start=[117.6,31.],end=[117.61,31.],source='wrf',level='300m MSL',
    planner_type='wrf_v3',departure_at='2026-11-04T02:00:00Z')
request,dataset=v3_service.prepare_plan(request)
manager=RouteJobManager(workers=1,timeout_seconds=8)
first=None
started=monotonic()
try:
    state=manager.submit(lambda request,publish:v3_service.compute_plan(request,publish,dataset),request,
                         on_finish=v3_service.finalize_plan)
    while state['status'] not in TERMINAL:
        if state['has_result'] and first is None:first=monotonic()-started
        sleep(.1)
        state=manager.snapshot(state['job_id'])
    manager.jobs[state['job_id']].future.result(timeout=5)
    assert state['status']=='timed_out',{'status':state['status'],'message':state['message']}
    assert state['feasible'] and state['result']['analysis']['navigation_allowed']
    assert state['result'].get('window_job_id')
    report={'status':state['status'],'first_feasible_seconds':first,'elapsed_seconds':state['elapsed_seconds'],
        'retained_trajectory_id':state['result']['trajectory_id'],'automatic_window_job':state['result']['window_job_id'],
        'budget_seconds':8,'production_default_budget_seconds':600}
    work=Path(os.getenv('V3_VERIFICATION_DIR',str(ROOT/'data/v3_integration_tests')))
    work.mkdir(parents=True,exist_ok=True)
    (work/'timeout-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)
finally:
    manager.shutdown()
    v3_service.shutdown()
