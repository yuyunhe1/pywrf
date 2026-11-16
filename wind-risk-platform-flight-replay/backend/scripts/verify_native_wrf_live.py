"""Real native batch rollout checks; ephemeral jobs only, no route saves."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import build_opener,ProxyHandler,Request
from uuid import uuid4

import websockets

ROOT=Path(__file__).resolve().parents[3]
WORK=ROOT/'data/native_wrf_10min_tests'
HTTP=build_opener(ProxyHandler({}))
LOCAL='http://127.0.0.1:8001'
MODEL='NATIVE_WRF_TEN_MINUTE_HOLD_V1'


def api(path,body=None,method=None,base=LOCAL):
    request=Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json'},method=method)
    with HTTP.open(request,timeout=90) as response:return json.load(response)


def wind(dataset,at,base=LOCAL):
    return api('/api/flight-replay/wind?'+urlencode(dict(dataset_id=dataset,at=at.isoformat())),base=base)


async def main():
    runtime=json.loads((ROOT/'data/platform_runtime/v3-running.json').read_text(encoding='utf-8-sig'))
    public=runtime['public_url']
    performance=json.loads((WORK/'real-performance.json').read_text(encoding='utf-8'))
    sample=performance['short_routes'][0]
    before=api('/api/routes')
    report=dict(writes_platform_history=False,checks=[],public_url=public)
    for base in (LOCAL,'http://127.0.0.1:5174',public):
        health=await asyncio.to_thread(api,'/api/health',base=base)
        available=await asyncio.to_thread(api,'/api/times?source=wrf&auto_download=false',base=base)
        assert available['wind_time_model']==MODEL and available['output_interval_seconds']==600
        rows=[r for r in available['valid_times'] if r['cycle']==performance['cycle']]
        assert len(rows)==289 and sorted(r['forecast_minutes'] for r in rows)==list(range(0,2881,10))
        report['checks'].append(dict(base=base,health=health,frames=len(rows),model=MODEL))
    departure=datetime.fromtimestamp((int(datetime.now(timezone.utc).timestamp())//60+2)*60,timezone.utc)
    geometry=dict(points=[sample['start'],sample['end']],source='wrf',level='100m AGL',
                  cycle=performance['cycle'],average_ground_speed_mps=10)
    windows=await asyncio.to_thread(api,'/api/route/departure-windows',geometry)
    profile=await asyncio.to_thread(api,'/api/route/flight-profile',dict(geometry,
        departure_at=departure.isoformat(),dataset_id=windows['dataset_id']))
    assert windows['wind_time_model']==profile['wind_time_model']==MODEL
    assert windows['dataset_id']==profile['dataset_id'] and len(profile['frame_times'])==289
    start=datetime.strptime(performance['cycle'],'%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
    frames=[await asyncio.to_thread(wind,profile['dataset_id'],start+timedelta(seconds=seconds))
            for seconds in (0,180,599.999,600)]
    assert all(f['available'] and f['metadata']['wind_time_model']==MODEL for f in frames)
    assert frames[0]['velocity']==frames[1]['velocity']==frames[2]['velocity']
    assert frames[3]['velocity']!=frames[0]['velocity']
    outside=await asyncio.to_thread(wind,profile['dataset_id'],start+timedelta(hours=48,seconds=1))
    assert not outside['available']
    public_frame=await asyncio.to_thread(wind,profile['dataset_id'],start+timedelta(seconds=180),public)
    assert public_frame['velocity']==frames[0]['velocity']
    minute_map=await asyncio.to_thread(api,'/api/wind?'+urlencode(dict(source='wrf',cycle=performance['cycle'],
        forecast_minutes=10,level='100m AGL')),base=public)
    assert minute_map['metadata']['forecast_minutes']==10 and minute_map['metadata']['forecast_hour']==1/6
    assert minute_map['metadata']['wind_time_model']==MODEL
    report.update(dataset_id=profile['dataset_id'],cycle=performance['cycle'],
        hold_boundaries_verified=[0,180,599.999,600],no_extrapolation_verified=True,
        public_minute_map_verified=True,window_count=len(windows['departure_windows']),
        profile_status=profile['decision']['status'])
    historical=await asyncio.to_thread(api,'/api/route/flight-profile',dict(geometry,
        cycle='2026-11-08 06:00 UTC',departure_at='2026-11-08T07:00:00Z'))
    assert historical['decision']['status']=='unknown' and '原生 10 分钟' in historical['decision']['reason']
    report['historical_hourly_rejected']=True
    body=dict(source='wrf',cycle=performance['cycle'],forecast_minutes=0,level='100m AGL',
        time_aware=True,start=sample['start'],end=sample['end'],departure_at=departure.isoformat(),
        average_ground_speed_mps=10,planner_type='wa_lpa_star',planning_strategy='wind_avoidance',
        client_instance_id='native-rollout-'+uuid4().hex,client_request_sequence=1)
    try:
        await asyncio.to_thread(api,'/api/route/plan/jobs',dict(body,planner_type='wrf_v3'))
        raise AssertionError('V3 new planning must be paused')
    except HTTPError as exc:
        detail=exc.read().decode('utf-8')
        assert exc.code==400 and '暂停新规划' in detail
    report['v3_new_planning_paused']=True
    # V3 failed creation belongs to its own sequence; the classic request advances it.
    body['client_request_sequence']=2
    submission_began=time.monotonic()
    state=await asyncio.to_thread(api,'/api/route/plan/jobs',body,base=public)
    path='/api/route/plan/jobs/'+state['job_id']
    report['job_id']=state['job_id'];beats=[]
    socket_url=public.replace('https://','wss://').replace('http://','ws://')+path+'/owner'
    try:
        async with websockets.connect(socket_url,proxy=None,open_timeout=25,max_size=None) as socket:
            await socket.send(json.dumps({'client_instance_id':body['client_instance_id']}))
            async def heartbeats():
                async for raw in socket:
                    beat=json.loads(raw)
                    assert beat['type']=='heartbeat'
                    beats.append(beat['seq'])
                    await socket.send(json.dumps({'type':'ack','seq':beat['seq']}))
            task=asyncio.create_task(heartbeats())
            began=time.monotonic()
            try:
                while time.monotonic()-began<45:
                    state=await asyncio.to_thread(api,path)
                    if state.get('result') and (beats or state['status']=='completed'):break
                    if state['status'] in {'failed','timed_out','cancelled'}:raise AssertionError(state['message'])
                    await asyncio.sleep(.25)
                result=state.get('result') or {}
                assert result.get('wind_time_model')==MODEL and result['flight_profile']['wind_time_model']==MODEL,state
                assert result['analysis']['navigation_allowed'] and result['dataset_id']==profile['dataset_id']
                report.update(public_owner_heartbeats=beats,first_feasible_seconds=state['first_feasible_elapsed_seconds'],
                    first_result_observed_seconds=time.monotonic()-submission_began,route_distance_km=result['distance_km'],
                    queue_wait_seconds=state['queue_wait_seconds'],preparation_seconds=state['preparation_seconds'])
                began=time.monotonic()
                cancelled=await asyncio.to_thread(api,path,method='DELETE')
                while not cancelled['compute_finished'] and time.monotonic()-began<2:
                    await asyncio.sleep(.025);cancelled=await asyncio.to_thread(api,path)
                assert cancelled['compute_finished'],cancelled
                report.update(cancel_seconds=time.monotonic()-began,cancel_status=cancelled['status'])
            finally:
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
    finally:
        await asyncio.to_thread(api,path,method='DELETE')
        await asyncio.to_thread(api,'/api/route/planning-clients/'+body['client_instance_id']+'?through=2&forget=true',method='DELETE')
    assert api('/api/routes')==before,'Smoke must not alter saved history'
    report.update(history_unchanged=True,verified_at=datetime.now(timezone.utc).isoformat())
    (WORK/'production-readiness.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':asyncio.run(main())
