"""Read-only HTTP acceptance for comparison during segmented display."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from time import monotonic, sleep
from uuid import uuid4

from verify_first_feasible_http import fetch

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT/'data/segment_comparison_tests'


def main():
    runtime=json.loads((ROOT/'data/platform_runtime/v3-running.json').read_text(encoding='utf-8-sig'))
    context=json.loads((WORK/'wrf-strategy-context.json').read_text(encoding='utf-8'))
    endpoints=json.loads((ROOT/'data/strategy_saved_tests/saved-cases.json').read_text(encoding='utf-8'))[0]
    departure=(datetime.now(timezone.utc)+timedelta(minutes=2)).replace(second=0,microsecond=0).isoformat()
    base='http://127.0.0.1:8001'; cases=[]
    for path in ('/api/route/plan/jobs','/api/route/recommendation/jobs'):
        client='segment-comparison-http-'+uuid4().hex
        body=dict(client_instance_id=client,client_request_sequence=1,start=endpoints['start'],end=endpoints['end'],
            source='wrf',cycle=context['cycle'],level='100m AGL',departure_at=departure,time_aware=True,
            average_ground_speed_mps=10,planner_type='wa_lpa_star',planning_strategy='wind_avoidance',
            optimize_during_display=True,display_segment_seconds=2,stop_on_first_feasible=False)
        if 'recommendation' in path: body['mode']='specified_time'
        revisions=[]; began=monotonic();first=None;dataset=None;last_revision=-1
        try:
            state=fetch(base+path,'POST',body);identity=state['job_id']
            until=None
            while True:
                assert monotonic()-began<35,state.get('message')
                if state['result'] and state['revision']!=last_revision:
                    value=state['result'];last_revision=state['revision']
                    assert value['analysis']['navigation_allowed'] and value['objective_model']=='SUPERCOVER_WIND_EXPOSURE_V5'
                    assert not value['stop_on_first_feasible']
                    dataset=dataset or value['dataset_id'];assert dataset==value['dataset_id']
                    revisions.append(dict(revision=last_revision,seconds=monotonic()-began,objective_cost=value['objective_cost'],
                        geometry=hashlib.sha256(json.dumps(value['points']).encode()).hexdigest(),distance_km=value['distance_km']))
                if state['feasible'] and first is None:
                    first=monotonic()-began;until=monotonic()+8
                if state['compute_finished'] or until is not None and monotonic()>=until: break
                sleep(.2);state=fetch(base+path+'/'+identity)
            assert first is not None,state
            before=monotonic()
            freeze=fetch(base+path+'/'+identity+'/finish','POST')
            while not state['compute_finished']:
                assert monotonic()-before<3,'comparison workers did not release'
                sleep(.05);state=fetch(base+path+'/'+identity)
            release_seconds=monotonic()-before
            assert state['status']=='completed' and state['feasible'],state
            if freeze['status']=='running': assert state['result']['planning_termination']=='display_finished'
            if 'workflow' in state:
                assert state['workflow']['stage']=='completed' and len(state['workflow']['attempts'])==1
                assert not state['workflow']['recommendations']
            sleep(.3)
            assert fetch(base+path+'/'+identity)['revision']==state['revision']
            public=fetch(runtime['public_url']+path+'/'+identity+'/finish','POST')
            assert public['status']=='completed' and public['compute_finished']
            assert all(a['objective_cost']>=b['objective_cost'] for a,b in zip(revisions,revisions[1:]))
            cases.append(dict(endpoint=path,job_id=identity,status=state['status'],compute_finished=True,
                first_observed_seconds=first,release_after_finish_seconds=release_seconds,revision=state['revision'],
                revisions=revisions,distinct_routes=len({row['geometry'] for row in revisions}),
                final_distance_km=state['result']['distance_km'],dataset_id=dataset,departure_at=departure,
                public_finish_status=public['status'],writes_history=False))
        finally:
            released=fetch(base+'/api/route/planning-clients/'+client+'?through=1&forget=true','DELETE')
            deadline=monotonic()+3
            while not released['compute_finished']:
                assert monotonic()<deadline
                sleep(.05);released=fetch(base+'/api/route/planning-clients/'+client+'?through=1&forget=true','DELETE')
    release=json.loads((ROOT/'wind-risk-platform-flight-replay/frontend/.releases/current.json').read_text(encoding='utf-8'))
    checks=[]
    for origin in (base,'http://127.0.0.1:5174',runtime['public_url']):
        health=fetch(origin+'/api/health')
        assert health['status']=='ok' and health['download']['interval_seconds']==1800
        assert health['resource_limits']['max_search_subprocesses']==4
        row=dict(origin=origin,health='ok',gfs_check_interval_seconds=1800)
        if origin!=base:
            html=fetch(origin+'/?segment-comparison='+uuid4().hex)
            asset=re.search(r'src="(/assets/index-[^"]+\.js)"',html);assert asset
            script=fetch(origin+asset[1])
            assert all(term in script for term in ('optimize_during_display','display_segment_seconds','/finish'))
            assert (ROOT/'wind-risk-platform-flight-replay/frontend/.releases'/release['directory']/asset[1].lstrip('/')).exists()
            row.update(asset=asset[1],source_id=release['source_id'])
        checks.append(row)
    report=dict(verified_at=datetime.now(timezone.utc).isoformat(),cases=cases,checks=checks,release=release,
        manual_browser_acceptance='pending_user',computer_use=False,writes_history=False)
    (WORK/'production-readiness.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__': main()
