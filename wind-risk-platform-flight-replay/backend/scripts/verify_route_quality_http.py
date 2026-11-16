"""Exercise deployed guards and future fallback without browser/history writes."""
import json
from pathlib import Path
import re
import sys
from time import monotonic, sleep
from uuid import uuid4

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
sys.path.insert(0,str(BACKEND))
from app.route_quality import assess_route_quality, MODEL
from verify_first_feasible_http import fetch

WORK=ROOT/'data/route_quality_tests'
BASE='http://127.0.0.1:8001'


def main():
    case=json.loads((BACKEND/'tests/fixtures/route_quality_user_20261115.json').read_text(encoding='utf-8'))
    runtime=json.loads((ROOT/'data/platform_runtime/v3-running.json').read_text(encoding='utf-8-sig'))
    runs=[]
    for kind in ('plan','recommendation'):
        client='quality-http-'+uuid4().hex
        path=f'/api/route/{kind}/jobs'
        body={key:case[key] for key in ('source','level','cycle','departure_at','average_ground_speed_mps','planner_type','planning_strategy')}
        body.update(start=case['points'][0],end=case['points'][-1],client_instance_id=client,client_request_sequence=1,
            time_aware=True,prefer_simple_route=True,route_quality_extra_seconds=10,
            optimize_during_display=True,display_segment_seconds=2)
        if kind=='plan': body['seed_routes']=[case['points']] # Explicitly test protection against an old bad cached route.
        else: body['mode']='specified_time'
        observed=[];began=monotonic();cards=[];identity=None;final=None
        try:
            state=fetch(BASE+path,'POST',body);identity=state['job_id']
            while True:
                assert monotonic()-began<50,'deployed quality workflow did not reach a verified outcome'
                quality=state.get('route_quality')
                if quality and quality['status']=='searching_alternative':
                    observed.append(dict(seconds=monotonic()-began,remaining=quality['remaining_seconds']))
                value=state.get('result') or {}
                if value.get('points'):
                    assert value['analysis']['navigation_allowed']
                    assert value['display_quality']['model']==MODEL and value['display_quality']['acceptable']
                    assert assess_route_quality(value['points'],10)['acceptable']
                if kind=='plan' and state['compute_finished']:
                    if not state['feasible']:
                        assert value['route_status']=='quality_unconfirmed'
                        assert value['analysis']['navigation_status']=='unknown' and not value['points']
                    final=state;break
                cards=state.get('workflow',{}).get('recommendations',[])
                if any(card.get('certification')=='windows_confirmed' and card.get('windows',{}).get('departure_windows') for card in cards):
                    for card in cards:assert assess_route_quality(card['plan']['points'],10)['acceptable']
                    final=state;break
                assert state['status'] not in {'failed','cancelled','timed_out'},state.get('message')
                sleep(.25);state=fetch(BASE+path+'/'+identity)
            assert observed,'extra search was never observable'
            (WORK/f'http-{kind}-outcome.json').write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding='utf-8')
            runs.append(dict(kind=kind,job_id=identity,elapsed_seconds=monotonic()-began,extra_search_observed=observed,
                final_status=final['status'],feasible=final['feasible'],captured_bad_seed=kind=='plan',
                recommendations=[dict(departure=c['first_departure_at'],distance_km=c['plan']['distance_km'],
                    windows=c['windows']['departure_windows'][:2]) for c in cards],
                test_stop_after_first_confirmed_recommendation=kind=='recommendation'))
            public=fetch(runtime['public_url']+path+'/'+identity)
            assert public['job_id']==identity
        finally:
            until=monotonic()+3
            while True:
                released=fetch(BASE+'/api/route/planning-clients/'+client+'?through=1&forget=true','DELETE')
                if released['compute_finished']:break
                assert monotonic()<until,'owned test computation did not release'
                sleep(.05)
    release=json.loads((ROOT/'wind-risk-platform-flight-replay/frontend/.releases/current.json').read_text(encoding='utf-8'))
    checks=[]
    for origin in (BASE,'http://127.0.0.1:5174',runtime['public_url']):
        health=fetch(origin+'/api/health')
        assert health['status']=='ok' and health['download']['interval_seconds']==1800
        assert health['resource_limits']['max_search_subprocesses']==4
        check=dict(origin=origin,health='ok',gfs_check_interval_seconds=1800)
        if origin!=BASE:
            html=fetch(origin+'/?route-quality='+uuid4().hex)
            asset=re.search(r'src="(/assets/index-[^"]+\.js)"',html);assert asset
            script=fetch(origin+asset[1])
            assert all(term in script for term in ('prefer_simple_route','route_quality_extra_seconds',MODEL))
            assert (ROOT/'wind-risk-platform-flight-replay/frontend/.releases'/release['directory']/asset[1].lstrip('/')).exists()
            check.update(asset=asset[1],source_id=release['source_id'])
        checks.append(check)
    report=dict(source_job_id=case['job_id'],runs=runs,checks=checks,release=release,writes_history=False,
        manual_browser_acceptance='pending_user',computer_use=False)
    (WORK/'production-readiness.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({**report,'runs':[{k:v for k,v in row.items() if k!='extra_search_observed'} for row in runs]},ensure_ascii=True,indent=2))


if __name__=='__main__':main()
