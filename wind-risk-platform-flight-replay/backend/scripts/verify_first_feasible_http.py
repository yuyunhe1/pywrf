"""Verify the deployed services through HTTP; no browser or history writes."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'data/first_feasible_tests'


def fetch(url, method='GET', body=None):
    payload = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(url, data=payload, method=method, headers={'Content-Type':'application/json'}), timeout=25) as response:
        raw = response.read()
        return json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw.decode()


def main():
    runtime = json.loads((ROOT/'data/platform_runtime/v3-running.json').read_text(encoding='utf-8-sig'))
    base = 'http://127.0.0.1:8001'
    cases = []
    endpoints = json.loads((ROOT/'data/strategy_saved_tests/saved-cases.json').read_text(encoding='utf-8'))[0]
    cycle = json.loads((WORK/'real-summary.json').read_text(encoding='utf-8'))[0]['cycle']
    departure = (datetime.now(timezone.utc)+timedelta(minutes=2)).replace(second=0,microsecond=0).isoformat()
    for path in ('/api/route/plan/jobs','/api/route/recommendation/jobs'):
        client = 'first-feasible-http-'+uuid4().hex
        body = dict(client_instance_id=client,client_request_sequence=1,start=endpoints['start'],end=endpoints['end'],
            cycle=cycle,source='wrf',level='100m AGL',time_aware=True,departure_at=departure,
            average_ground_speed_mps=10,planning_strategy='wind_avoidance',planner_type='wa_lpa_star',stop_on_first_feasible=True)
        if 'recommendation' in path: body['mode']='specified_time'
        began = monotonic(); first = None
        try:
            state = fetch(base+path,'POST',body)
            job = state['job_id']
            while not state['compute_finished']:
                assert monotonic()-began < 45, state.get('message')
                if state['feasible'] and first is None: first=monotonic()-began
                sleep(.1)
                state=fetch(base+path+'/'+job)
            assert state['status']=='completed' and state['feasible'],state
            result=state['result']
            assert result['analysis']['navigation_allowed'] and result['stop_on_first_feasible']
            attempt=result['planning_diagnostics']['attempts'][0]
            assert attempt['stop_reason']=='first_feasible' and attempt['complete_candidates']==1
            if 'workflow' in state:
                assert state['workflow']['stage']=='completed' and len(state['workflow']['attempts'])==1
                assert not state['workflow']['recommendations']
            sleep(.5)
            later=fetch(base+path+'/'+job)
            assert later['revision']==state['revision'] and later['compute_finished'] and later['status']=='completed'
            cases.append(dict(endpoint=path,job_id=job,status=state['status'],compute_finished=True,revision=state['revision'],
                http_completion_seconds=monotonic()-began-.5,first_observed_seconds=first,
                backend_first_feasible_elapsed_seconds=state['first_feasible_elapsed_seconds'],
                worker_release_seconds=attempt['worker_release_seconds'],stop_reason=attempt['stop_reason'],
                distance_km=result['distance_km'],dataset_id=result['dataset_id'],departure_at=departure,writes_history=False))
        finally:
            released=fetch(base+'/api/route/planning-clients/'+client+'?through=1&forget=true','DELETE')
            assert released['compute_finished']
    release=json.loads((ROOT/'wind-risk-platform-flight-replay/frontend/.releases/current.json').read_text(encoding='utf-8'))
    checks=[]
    for origin in (base,'http://127.0.0.1:5174',runtime['public_url']):
        health=fetch(origin+'/api/health')
        assert health['status']=='ok' and health['download']['interval_seconds']==1800
        assert health['resource_limits']['max_search_subprocesses']==4
        row=dict(origin=origin,health='ok',gfs_check_interval_seconds=health['download']['interval_seconds'])
        if origin!=base:
            html=fetch(origin+'/?first-feasible='+uuid4().hex)
            asset=re.search(r'src="([^"]+/assets/index-[^"]+\.js|/assets/index-[^"]+\.js)"',html)
            assert asset,html[:300]
            script=fetch(origin+asset[1])
            assert 'stop_on_first_feasible' in script and 'preview_invalidated' in script
            assert (ROOT/'wind-risk-platform-flight-replay/frontend/.releases'/release['directory']/asset[1].lstrip('/')).exists()
            row.update(asset=asset[1],source_id=release['source_id'])
        checks.append(row)
    report=dict(verified_at=datetime.now(timezone.utc).isoformat(),cases=cases,checks=checks,release=release,
        manual_browser_acceptance='pending_user',computer_use=False,writes_history=False)
    (WORK/'production-readiness.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__':
    main()
