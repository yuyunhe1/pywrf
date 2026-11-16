"""Additional real-data checks: manual headings, rejects, bounded windows."""
from datetime import datetime
import json
from pathlib import Path
import sys
from threading import Event
from time import monotonic
from urllib.request import Request,ProxyHandler,build_opener
import time

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
WORK=ROOT/'data/v3_integration_tests'
sys.path.insert(0,str(BACKEND))
sys.stdout.reconfigure(encoding='utf-8')
from app import v3_native,v3_service
from app.models import RoutePlanRequest
from app.planning_control import planning_scope,PlanningTimedOut


class First(Exception):
    def __init__(self,result):self.result=result


def main():
    report={}
    def save():
        (WORK/'runtime-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    dataset=v3_native.dataset(300)
    base={'start':[117.6,31.],'end':[117.61,31.],'source':'wrf','level':'300m MSL',
          'planner_type':'wrf_v3','departure_at':'2026-11-04T02:00:00Z','dataset_id':dataset['dataset_id']}
    began=monotonic()
    def publish(result):raise First(result)
    try:
        with planning_scope(monotonic()+120,Event()):
            v3_service.compute_plan(RoutePlanRequest(**{**base,'constrain_headings':True,'start_heading_deg':90.,'end_heading_deg':90.}),publish,dataset)
        raise AssertionError('Manual-heading solution not published')
    except First as found:
        samples=found.result['analysis']['samples']
        assert abs(samples[0]['flight_heading']-90)<.001
        assert abs(samples[-1]['flight_heading']-90)<.001
        report['manual_headings']={'first_seconds':monotonic()-began,'start_heading_deg':samples[0]['flight_heading'],
            'end_heading_deg':samples[-1]['flight_heading'],'trajectory_id':found.result['trajectory_id']}
        save()
        print(json.dumps({'manual_headings':report['manual_headings']}),flush=True)
    for name,changes,expected in [('terrain_msl',{'altitude_msl_m':20,'dataset_id':None},('BELOW_TERRAIN','BELOW_VERTICAL_DOMAIN')),
                                  ('outside_domain',{'start':[110.,25.]},('OUT_OF_ROI','OUT_OF_HORIZONTAL_DOMAIN'))]:
        began=monotonic()
        try:
            with planning_scope(monotonic()+30,Event()):
                v3_service.compute_plan(RoutePlanRequest(**{**base,**changes}))
            raise AssertionError('Invalid input accepted')
        except ValueError as exc:
            assert any(code in str(exc) for code in expected),str(exc)
            report[name]={'seconds':monotonic()-began,'message':str(exc)}
            save()
            print(json.dumps({name:report[name]},ensure_ascii=False),flush=True)
    long_plan=json.loads((WORK/'long_87km-plan.json').read_text(encoding='utf-8'))
    info=v3_service.trajectory(long_plan['trajectory_id'])
    request={**info['platform_constraints'],'trajectory_id':long_plan['trajectory_id'],
             'departure_at':'2026-11-04T02:00:00Z','evaluated_at_epoch':datetime.fromisoformat('2026-11-04T02:00:00+00:00').timestamp()}
    held=[]
    began=monotonic()
    try:
        with planning_scope(monotonic()+65,Event()):
            result=v3_service.run_worker('windows',request,v3_native.get_dataset(long_plan['dataset_id']),held.append,budget_seconds=60)
        outcome='completed'
    except PlanningTimedOut:
        assert held,'A bounded window task must publish current decision and retain partials'
        result=held[-1]
        outcome='timed_out'
    assert result['trajectory_id']==long_plan['trajectory_id']
    for window in result['departure_windows']:
        assert window['certification']=='CONSERVATIVE_CONTINUOUS'
        assert datetime.fromisoformat(window['start'].replace('Z','+00:00')).timestamp()%300==0
        assert datetime.fromisoformat(window['end'].replace('Z','+00:00')).timestamp()%300==0
    report['long_windows']={'status':outcome,'seconds':monotonic()-began,'updates':len(held),
        'confirmed_windows':result['departure_windows'],'complete':result['complete'],
        'unconfirmed_intervals':result['unconfirmed_intervals'],'certificate_boxes':result['certificate_boxes']}
    save()
    (WORK/'long-window-partial.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'long_windows':report['long_windows']},ensure_ascii=False),flush=True)
    # Check the real HTTP job manager, including automatic window start. The
    # isolated server deliberately uses 22 s to exercise retained timeouts.
    opener=build_opener(ProxyHandler({}))
    def api(path,body=None):
        data=json.dumps(body).encode() if body is not None else None
        return json.loads(opener.open(Request('http://127.0.0.1:8002/api/'+path,data=data,
            headers={'Content-Type':'application/json'}),timeout=20).read())
    job=api('route/plan/jobs',base)
    best=None
    while job['status'] not in ('completed','timed_out','failed','cancelled'):
        if job.get('result'):best=job['result']
        time.sleep(.25)
        job=api('route/plan/jobs/'+job['job_id'])
    best=job.get('result') or best
    assert job['status'] in ('completed','timed_out') and best['analysis']['navigation_allowed'],{k:job[k] for k in ('status','message','elapsed_seconds')}
    assert best.get('window_job_id'),best
    report['http_finalization']={'status':job['status'],'elapsed_seconds':job['elapsed_seconds'],
        'retained_trajectory_id':best['trajectory_id'],'automatic_window_job':best['window_job_id']}
    (WORK/'runtime-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'http_finalization':report['http_finalization']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
