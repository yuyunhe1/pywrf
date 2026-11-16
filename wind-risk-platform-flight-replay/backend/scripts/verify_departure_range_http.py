"""Verify the deployed range API/proxy/release without browser automation."""
from datetime import datetime, timezone, timedelta
import json
import math
import os
from pathlib import Path
import re
import sys
from time import monotonic, sleep
from urllib.request import Request, urlopen
from uuid import uuid4

import numpy as np

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
sys.path.insert(0,str(BACKEND))
os.environ.update(WRF_NATIVE_10MIN_REQUIRED='1',GFS_AUTO_DOWNLOAD='0',WRF_CACHE_AUTO_SYNC_INDEX='0',
    WRF_CACHE_REMOTE_HOST='',WIND_FRAME_WARMUP='0')
from app.models import RecommendationJobRequest, FlightProfileRequest
from app.local_weather import LocalWeather, preserves_waypoints
from app.flight_replay import build_profile
from app.range_evaluation import RouteRangeEvaluation, SELECTION_MODEL
from app.route_context import geometry_route
from app.wrf_timeline import load_dataset, bracket

BASE='http://127.0.0.1:8001'


def fetch(url,method='GET',body=None):
    data=json.dumps(body).encode() if body is not None else None
    with urlopen(Request(url,data=data,method=method,headers={'Content-Type':'application/json'}),timeout=90) as response:
        raw=response.read().decode('utf-8')
        return json.loads(raw) if 'json' in response.headers.get('Content-Type','') else raw


def main():
    runtime=json.loads((ROOT/'data/platform_runtime/v3-running.json').read_text(encoding='utf-8-sig'))
    times=fetch(BASE+'/api/times?source=wrf&auto_download=false')
    cycle=max(row['cycle'] for row in times['valid_times'])
    now=datetime.now(timezone.utc)
    stamp=math.ceil((now.timestamp()+3600)/600)*600
    departure=datetime.fromtimestamp(stamp,timezone.utc)
    data=load_dataset('100m AGL',now,cycle)
    pair=bracket(data,stamp)
    assert pair is not None,'future native frame unavailable'
    grid=pair[0]
    magnitude=np.hypot(np.asarray(grid.u),np.asarray(grid.v))
    inner=magnitude.copy()
    for dr in (-1,0,1):
        for dc in (-1,0,1):
            inner=np.maximum(inner,np.roll(np.roll(magnitude,dr,axis=0),dc,axis=1))
    inner[[0,-1],:]=math.inf;inner[:,[0,-1]]=math.inf
    r,c=np.unravel_index(np.nanargmin(inner),inner.shape)
    assert inner[r,c]<7.,'no suitable isolated short-route test cell'
    lon,lat=float(grid.lons[c]),float(grid.lats[r])
    points=[[lon-.0005,lat],[lon,lat],[lon+.0005,lat]]
    client='departure-range-http-'+uuid4().hex
    request=dict(mode='departure_range',source='wrf',cycle=cycle,level='100m AGL',required_waypoints=points,
        departure_start_at=departure.isoformat(),departure_end_at=(departure+timedelta(minutes=8)).isoformat(),
        average_ground_speed_mps=10.,planner_type='wa_lpa_star',prefer_simple_route=True,
        client_instance_id=client,client_request_sequence=1)
    before=fetch(BASE+'/api/routes')
    began=monotonic();seen=[];identity=None;last=None
    try:
        state=fetch(BASE+'/api/route/recommendation/jobs','POST',request)
        identity=state['job_id']
        while True:
            if state.get('result'):
                result=state['result']
                assert result['mode']=='departure_range' and result['analysis']['navigation_allowed']
                assert preserves_waypoints(result['points'],points)
                assert result['max_contact_wind_speed_mps']<=result['min_verified_peak_mps']+1.
                assert departure<=datetime.fromisoformat(result['departure_at'].replace('Z','+00:00'))<=departure+timedelta(minutes=8)
                seen.append(dict(seconds=monotonic()-began,revision=state['revision'],departure_at=result['departure_at']))
            if state['status'] in {'completed','failed','timed_out','cancelled'}:
                last=state
                break
            assert monotonic()-began<50,'deployed short range task exceeded isolated HTTP budget'
            sleep(.2)
            state=fetch(BASE+'/api/route/recommendation/jobs/'+identity)
        assert last['status']=='completed',last['message']
        result=last['result']
        assert result and result['range_comparison_complete']
        assert result['dataset_id']==data.dataset_id
        q=RecommendationJobRequest(**request).model_copy(update={
            'departure_at':datetime.fromisoformat(result['departure_at'].replace('Z','+00:00'))})
        check=build_profile(geometry_route(result['points'],q.level,source=q.source),FlightProfileRequest(
            departure_at=q.departure_at,average_ground_speed_mps=10,thresholds=q.thresholds,wind_shear=q.wind_shear),data)
        weather=LocalWeather(data,q,{},loading_mode='local')
        peak=weather.contact_peak(result['points'])
        assert check['analysis']['navigation_allowed'] and abs(peak-result['max_contact_wind_speed_mps'])<1e-7
        material=RouteRangeEvaluation(result['points'],data,q,departure.timestamp(),(departure+timedelta(minutes=8)).timestamp())
        windows=material.windows(result['peak_wind_limit_mps'],now.timestamp())
        assert windows['departure_windows']==result['departure_windows']['departure_windows']
        assert any(row['end']>row['start'] for row in windows['departure_windows'])
        public=fetch(runtime['public_url']+'/api/route/recommendation/jobs/'+identity)
        assert public['job_id']==identity and public['result']['selection_model']==SELECTION_MODEL
    finally:
        end=monotonic()+3
        while True:
            released=fetch(BASE+'/api/route/planning-clients/'+client+'?through=1&forget=true','DELETE')
            if released['compute_finished']:break
            assert monotonic()<end,'test task did not release its search resources'
            sleep(.05)
    after=fetch(BASE+'/api/routes')
    assert {r['route_id'] for r in before}=={r['route_id'] for r in after},'history changed during the read-only acceptance'
    release=json.loads((ROOT/'wind-risk-platform-flight-replay/frontend/.releases/current.json').read_text(encoding='utf-8'))
    checks=[]
    for origin in (BASE,'http://127.0.0.1:5174',runtime['public_url']):
        health=fetch(origin+'/api/health')
        assert health['status']=='ok' and health['download']['interval_seconds']==1800
        assert health['resource_limits']['max_search_subprocesses']==4
        row=dict(origin=origin,health='ok',max_search_subprocesses=4,gfs_poll_seconds=1800)
        if origin!=BASE:
            html=fetch(origin+'/?departure-range='+uuid4().hex)
            asset=re.search(r'src="(/assets/index-[^"]+\.js)"',html)
            assert asset
            script=fetch(origin+asset[1])
            assert all(term in script for term in ('departure_range','required_waypoints','起飞时间段限制',SELECTION_MODEL))
            served=fetch(origin+'/release.json')
            assert served['source_id']==release['source_id']
            row.update(asset=asset[1],source_id=release['source_id'])
        checks.append(row)
    target=ROOT/'data/departure_range_tests'
    target.mkdir(parents=True,exist_ok=True)
    report=dict(status='passed',job_id=identity,request=request,result=last,observations=seen,
        independent_peak_mps=peak,independent_windows=windows['departure_windows'],checks=checks,
        resource_release=released,writes_history=False,browser_automation=False,manual_browser_acceptance='pending_user')
    (target/'deployed-http.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(status='passed',elapsed_seconds=monotonic()-began,observed_revisions=len(seen),
        peak=peak,windows=windows['departure_windows'],checks=checks),ensure_ascii=True))


if __name__=='__main__':main()
