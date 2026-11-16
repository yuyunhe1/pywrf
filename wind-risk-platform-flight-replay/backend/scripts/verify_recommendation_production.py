"""Local/proxy/public workflow smoke; ephemeral jobs only, no history writes."""
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import build_opener,ProxyHandler,Request
from urllib.error import HTTPError
from uuid import uuid4

import websockets

ROOT=Path(__file__).resolve().parents[3]
WORK=Path(os.getenv('RECOMMENDATION_PRODUCTION_REPORT_DIR',str(ROOT/'data/recommendation_tests')))
HTTP=build_opener(ProxyHandler({}))
LOCAL='http://127.0.0.1:8001'


def api(path,body=None,method=None,base=LOCAL):
    request=Request(base+path,data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json'},method=method)
    with HTTP.open(request,timeout=90) as response: return json.load(response)


async def cancel(path):
    began=time.monotonic()
    state=await asyncio.to_thread(api,path,method='DELETE')
    while not state['compute_finished'] and time.monotonic()-began<2:
        await asyncio.sleep(.025)
        state=await asyncio.to_thread(api,path)
    assert state['compute_finished'],state
    return dict(status=state['status'],compute_finished=state['compute_finished'],seconds=time.monotonic()-began)


async def main():
    runtime=json.loads((ROOT/'data/platform_runtime/v3-running.json').read_text(encoding='utf-8-sig'))
    public=runtime['public_url']
    before=api('/api/routes')
    report=dict(writes_platform_history=False,public_url=public,health=api('/api/health'),
        proxy_health=api('/api/health',base='http://127.0.0.1:5174'),
        public_health=await asyncio.to_thread(api,'/api/health',base=public))
    body=dict(source='wrf',cycle='2026-11-08 06:00 UTC',level='100m AGL',
        departure_at='2026-11-08T15:00:00+08:00',start=[119.59949,29.78905],end=[119.49109,30.27691],
        average_ground_speed_mps=10,planner_type='wa_lpa_star',planning_strategy='wind_avoidance',
        client_instance_id='recommendation-public-'+uuid4().hex,client_request_sequence=1)
    state=await asyncio.to_thread(api,'/api/route/recommendation/jobs',body,base=public)
    path='/api/route/recommendation/jobs/'+state['job_id']
    report.update(job_id=state['job_id'],owner_timeout_seconds=state['owner_timeout_seconds'],
        timeout_seconds=state['timeout_seconds'],heartbeat_sequences=[])
    try:
        assert state['owner_timeout_seconds']==60 and state['timeout_seconds']==7200,state
        uri=public.replace('https://','wss://').replace('http://','ws://')+path+'/owner'
        async with websockets.connect(uri,proxy=None,open_timeout=25,max_size=None) as socket:
            await socket.send(json.dumps({'client_instance_id':body['client_instance_id']}))
            for _ in range(2):
                beat=json.loads(await asyncio.wait_for(socket.recv(),timeout=20))
                assert beat['type']=='heartbeat',beat
                await socket.send(json.dumps({'type':'ack','seq':beat['seq']}))
                report['heartbeat_sequences'].append(beat['seq'])
            state=await asyncio.to_thread(api,path)
            assert state['status']=='running',state['message']
            assert state['workflow']['mode']=='specified_time'
            stats=(state.get('search_progress') or {}).get('statistics',{})
            assert stats.get('execution_mode')=='parallel_processes',stats
            assert stats['weather']['weather_loading_mode']=='full'
            assert stats['weather']['spatial_risk_model']=='CLOSED_CELL_SUPERCOVER_V2'
            assert len({v['pid'] for v in stats['direction_workers'].values()})==2
            report['statistics']=stats
        # Cross-manager supersession is bound to the same page sequence. The
        # replacement waits for the same bounded fleet; it cannot retain the
        # old page's trees or result/profile payloads.
        replacement=await asyncio.to_thread(api,'/api/route/plan/jobs',{
            **body,'time_aware':True,'client_request_sequence':2},base=public)
        replacement_path='/api/route/plan/jobs/'+replacement['job_id']
        report['cancellation']=await cancel(path)
        superseded=await asyncio.to_thread(api,path)
        assert superseded['result'] is None and not superseded['workflow'].get('recommendations'),superseded
        page_path='/api/route/planning-clients/'+body['client_instance_id']+'?through=2&forget=true'
        began=time.monotonic()
        closed=await asyncio.to_thread(api,page_path,method='DELETE',base=public)
        while not closed['compute_finished'] and time.monotonic()-began<2:
            await asyncio.sleep(.025)
            closed=await asyncio.to_thread(api,page_path,method='DELETE')
        assert closed['compute_finished'],closed
        assert (await asyncio.to_thread(api,replacement_path))['result'] is None
        report['page_cancellation_seconds']=time.monotonic()-began
        try:
            await asyncio.to_thread(api,'/api/route/recommendation/jobs',body)
            raise AssertionError('Superseded sequence was accepted')
        except HTTPError as exc: assert exc.code==409
        report['cross_manager_supersession_verified']=True
        report['stale_post_rejected']=True
        # Old job endpoint remains available and shares the same bounded fleet.
        legacy={k:v for k,v in body.items() if k!='client_request_sequence'}
        legacy['client_instance_id']='legacy-smoke-'+uuid4().hex
        old=await asyncio.to_thread(api,'/api/route/plan/jobs',{**legacy,'time_aware':True})
        old_path='/api/route/plan/jobs/'+old['job_id']
        report['old_api_cancellation']=await cancel(old_path)
        with HTTP.open(public+'/src/components/ControlPanel.vue',timeout=30) as response:
            component=response.read().decode('utf-8')
        assert '指定时间规划航线' in component and '推荐时间规划航线' in component
        report['public_new_frontend_verified']=True
        assert api('/api/routes')==before,'Normal route history changed during read-only smoke'
        report['history_unchanged']=True
        report['verified_at']=datetime.now(timezone.utc).isoformat()
        WORK.mkdir(parents=True,exist_ok=True)
        (WORK/'production-readiness.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k!='statistics'},ensure_ascii=False))
    finally:
        await asyncio.to_thread(api,path,method='DELETE')


if __name__=='__main__': asyncio.run(main())
