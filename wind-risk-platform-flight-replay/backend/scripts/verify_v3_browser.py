"""Real browser acceptance against v3_smoke_server.py and Vite on 5175."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import os
import shutil
import time
from urllib.request import Request,ProxyHandler,build_opener
import websockets

ROOT=Path(__file__).resolve().parents[3]
WORK=Path(os.getenv('V3_TEST_WORK_DIR',str(ROOT/'data/v3_integration_tests/browser')))
opener=build_opener(ProxyHandler({}))
sys.stdout.reconfigure(encoding='utf-8')


async def main():
    saved=json.loads(opener.open('http://127.0.0.1:8002/api/routes').read())
    for row in saved:
        if row['name'] in ('V3 浏览器验收','V3 长航线浏览器验收'):
            opener.open(Request('http://127.0.0.1:8002/api/routes/'+row['route_id'],method='DELETE')).close()
    pages=json.loads(opener.open('http://127.0.0.1:9226/json/list').read())
    for old in pages:
        if old['type']=='page':opener.open('http://127.0.0.1:9226/json/close/'+old['id']).close()
    page=json.loads(opener.open(Request('http://127.0.0.1:9226/json/new?about:blank',method='PUT')).read())
    errors,requests,checks=[],[],[]
    report={}
    async with websockets.connect(page['webSocketDebuggerUrl'],max_size=None) as socket:
        sequence=0
        async def call(method,params=None):
            nonlocal sequence
            sequence+=1
            identifier=sequence
            await socket.send(json.dumps({'id':identifier,'method':method,'params':params or {}}))
            while True:
                event=json.loads(await socket.recv())
                if event.get('method')=='Runtime.exceptionThrown':errors.append(event['params'])
                if event.get('method')=='Network.requestWillBeSent':requests.append(event['params']['request'])
                if event.get('id')==identifier:
                    if 'error' in event:raise AssertionError(event['error'])
                    return event.get('result',{})
        async def js(code):
            value=await call('Runtime.evaluate',{'expression':code,'returnByValue':True,'awaitPromise':True})
            if 'exceptionDetails' in value:raise AssertionError(value['exceptionDetails'])
            return value['result'].get('value')
        async def wait(condition,label,seconds=35):
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                if await js('Boolean('+condition+')'):
                    checks.append(label);print('[OK] '+label,flush=True);return
                await asyncio.sleep(.2)
            raise AssertionError(label+'\n'+str(await js('document.body.innerText.slice(-7000)')))
        async def input_value(selector,value,event='input'):
            await js("(()=>{const e=document.querySelector("+json.dumps(selector)+");if(!e)throw Error('missing input');e.value="+json.dumps(value)+";e.dispatchEvent(new Event("+json.dumps(event)+",{bubbles:true}));})()")
        async def button(text):
            await js("(()=>{const e=[...document.querySelectorAll('button')].find(e=>e.textContent.trim().includes("+json.dumps(text)+"));if(!e)throw Error('missing button');e.click();})()")
        def creations():return [r for r in requests if r['method']=='POST' and r['url'].endswith('/api/route/plan/jobs')]
        try:
            await call('Page.enable');await call('Runtime.enable');await call('Network.enable')
            # Basemap availability is independent of the V3 acceptance. Avoid
            # external OSM timeouts starving localhost API connections.
            await call('Network.setBlockedURLs',{'urls':['*/api/map-tiles/*']})
            await call('Emulation.setDeviceMetricsOverride',{'width':1600,'height':1050,'deviceScaleFactor':1,'mobile':False})
            await call('Browser.setDownloadBehavior',{'behavior':'allow','downloadPath':str(WORK/'downloads')})
            await call('Page.navigate',{'url':'http://127.0.0.1:5175/'})
            await wait("document.querySelector('#app')?.__vue_app__?._instance?.setupState?.options?.v3_native",'V3 default and native status loaded')
            await js("window.s=document.querySelector('#app').__vue_app__._instance.setupState")
            assert await js("s.planner.algorithm==='wrf_v3' && !s.planner.constrainHeadings && s.planner.altitudeMslM===300")
            await input_value('input[placeholder="默认航线 A"]','V3 浏览器验收')
            await js("(()=>{const a=[...document.querySelectorAll('input[placeholder=\"经度, 纬度\"]')];a[0].value='117.6,31';a[1].value='117.61,31';a.forEach(e=>e.dispatchEvent(new Event('input',{bubbles:true})))})()")
            await input_value('[aria-label="规划起飞方式"]','scheduled','change')
            await wait("document.querySelector('[aria-label=\"规划起飞时间（北京时间）\"]')",'scheduled departure input available')
            await input_value('[aria-label="规划起飞时间（北京时间）"]','2026-11-04T10:00:00')
            began=time.monotonic()
            await button('生成规划航线')
            await wait("s.flightReplay.result?.trajectory_id && s.planner.points.length>2",'first feasible route, profile and curve visible before optimization ends',25)
            report['browser_first_solution_seconds']=time.monotonic()-began
            assert await js("s.planner.planning && s.flightReplay.result.analysis.navigation_allowed && s.flightReplay.result.level==='300m MSL'")
            report['trajectory_id']=await js('s.flightReplay.result.trajectory_id')
            original_departure=await js('s.flightReplay.result.departure_at')
            await js("window.originalGeometry=JSON.stringify(s.planner.points);window.originalDuration=s.flightReplay.result.flight_duration_seconds;window.originalHeading=s.flightReplay.result.analysis.samples[0].flight_heading")
            await button('停止优化，保留当前航线')
            await wait("!s.planner.planning && s.departureWindows.loading",'stop optimization retains route and starts fixed-path windows')
            await wait("s.flightReplay.mapWind?.metadata?.level==='300m MSL'",'native MSL wind map shares the trajectory dataset',40)
            assert await js('s.flightReplay.mapWind.metadata.dataset_id===s.flightReplay.result.dataset_id')
            await wait("s.departureWindows.result?.departure_windows?.length>0",'conservative continuous windows displayed asynchronously',150)
            window_count=await js('s.departureWindows.result.departure_windows.length')
            await js("document.querySelector('.departure-window-button').click()")
            await wait("s.flightReplay.result && !s.flightReplay.loading",'clicking a future window retimes the existing curve',45)
            await input_value('[aria-label="起飞时间（北京时间）"]','2026-11-04T11:00:00','change')
            await wait("s.flightReplay.result?.departure_at.startsWith('2026-11-04T03:00:00') && !s.flightReplay.loading",'manual future departure recalculates ground speed, ETA and compensated heading',45)
            assert await js("JSON.stringify(s.planner.points)===window.originalGeometry && s.flightReplay.result.trajectory_id==="+json.dumps(report['trajectory_id']))
            assert await js('Math.abs(s.flightReplay.result.flight_duration_seconds-window.originalDuration)>.01 && Math.abs(s.flightReplay.result.analysis.samples[0].flight_heading-window.originalHeading)>.001')
            assert len(creations())==1,'Selecting departure must not start a new route search'
            await wait("!s.flightReplay.mapLoading",'future departure map uses corresponding hourly frames',40)
            await js("document.querySelector('[_echarts_instance_]').scrollIntoView({block:'center'})")
            point=await js("(async()=>{const e=document.querySelector('[_echarts_instance_]');const c=(await import('/src/utils/charts.js')).getInstanceByDom(e);const i=Math.floor(s.displayAnalysis.samples.length/2);const q=s.displayAnalysis.samples[i];const xy=c.convertToPixel({gridIndex:0},[q.distance_km,q.wind_speed]);const r=e.getBoundingClientRect();return {x:r.x+xy[0],y:r.y+xy[1],i};})()")
            await call('Input.dispatchMouseEvent',{'type':'mousePressed','x':point['x'],'y':point['y'],'button':'left','clickCount':1})
            await call('Input.dispatchMouseEvent',{'type':'mouseReleased','x':point['x'],'y':point['y'],'button':'left','clickCount':1})
            await wait("s.selectedRouteSampleIndex!==null && document.querySelectorAll('[class*=routeSelection] path').length>=2",'actual chart click highlights the matching map position in red')
            await wait("!s.flightReplay.mapLoading",'chart click seeks to actual trajectory time',40)
            assert await js('Math.abs(s.flightReplay.seconds-s.displayAnalysis.samples[s.selectedRouteSampleIndex].elapsed_seconds)<.001')
            await button('单时刻剖面')
            await wait("s.flightReplay.mode==='snapshot' && s.analysis && s.metadata?.dataset_id===s.flightReplay.result.dataset_id && !s.loading",'snapshot analysis and map use same native MSL dataset',45)
            assert await js("s.metadata.level==='300m MSL'")
            await button('飞行过程剖面')
            await wait("s.flightReplay.mode==='flight'",'switching back preserves flight profile')
            await button('下载保存')
            await wait("s.savedRoutes.some(r=>r.name==='V3 浏览器验收' && r.v3?.trajectory_id)",'V3 geometry reference saved with backward-compatible route record')
            previous_navigation=await js('performance.timeOrigin')
            await call('Page.reload')
            await wait("performance.timeOrigin!=="+str(previous_navigation)+" && document.querySelector('#app')?.__vue_app__?._instance?.setupState?.savedRoutes?.some(r=>r.name==='V3 浏览器验收')",'saved V3 route survives browser reload')
            await js("window.s=document.querySelector('#app').__vue_app__._instance.setupState;[...document.querySelectorAll('.saved-route-button')].find(e=>e.textContent.includes('V3 浏览器验收')).click()")
            await wait("s.flightReplay.result?.trajectory_id==="+json.dumps(report['trajectory_id'])+" && !s.flightReplay.loading",'loading a saved route restores the continuous curve, MSL and decision',60)
            assert await js("s.fixedRouteSelection.v3.endpoint_tolerance_m===1 && s.flightReplay.result.time_model==='constant_airspeed'")
            assert await js("document.querySelector('[aria-label=\"当前 V3 风场高度\"]').value==='300m MSL'")
            outside=await js("(async()=>{const last=Math.max(...s.flightReplay.result.frame_times.map(Date.parse));return (await import('/src/utils/flightReplay.js')).toBeijingInput(new Date(last+3600000).toISOString())})()")
            await input_value('[aria-label="起飞时间（北京时间）"]',outside,'change')
            await wait("s.flightReplay.result?.timing_available===false && s.flightReplay.result.decision.status==='unknown'",'forecast-end departure keeps geometry and reports missing coverage without a fabricated ETA',45)
            assert await js("s.flightReplay.result.arrival_at===null && s.flightReplay.result.analysis.samples.length===0 && s.planner.points.length>2")
            original_input=await js("(async()=> (await import('/src/utils/flightReplay.js')).toBeijingInput("+json.dumps(original_departure)+"))()")
            await input_value('[aria-label="起飞时间（北京时间）"]',original_input,'change')
            await wait("s.flightReplay.result?.analysis?.navigation_allowed && !s.flightReplay.loading",'original safe departure restores the same saved curve and timing',45)
            if '--long' in sys.argv:
                long_plan=json.loads((WORK.parent/'long_87km-plan.json').read_text(encoding='utf-8'))
                token=long_plan['trajectory_id']
                source=ROOT/'wind-risk-platform-flight-replay/data/wrf_v3_runtime/trajectories'
                target=WORK/'runtime/trajectories'
                for suffix in ('.npz','.json'):
                    shutil.copy2(source/(token+suffix),target/(token+suffix))
                payload={'name':'V3 长航线浏览器验收','points':long_plan['points'],'level':long_plan['level'],
                    'start':long_plan['points'][0][:2],'end':long_plan['points'][-1][:2],'v3':long_plan['v3']}
                opener.open(Request('http://127.0.0.1:8002/api/routes',data=json.dumps(payload).encode(),
                    headers={'Content-Type':'application/json'}),timeout=30).close()
                await js('s.refreshRoutes()')
                began=time.monotonic()
                await js("[...document.querySelectorAll('.saved-route-button')].find(e=>e.textContent.includes('V3 长航线浏览器验收')).click()")
                await wait("s.planner.points.length>1000",'long continuous route geometry appears before its evaluation finishes',20)
                report['long_geometry_display_seconds']=time.monotonic()-began
                await wait("s.flightReplay.result?.trajectory_id==="+json.dumps(token)+" && !s.flightReplay.loading",'real 87 km route renders its full risk profile and native MSL decision',100)
                report['long_profile_seconds']=time.monotonic()-began
                assert await js('s.flightReplay.result.analysis.navigation_allowed')
                report['long_map_segments']=await js('s.flightReplay.result.segments.length')
                report['long_profile_samples']=await js('s.flightReplay.result.analysis.samples.length')
                assert report['long_map_segments']<report['long_profile_samples']/10
            image=await call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
            (WORK/'v3-ui.png').write_bytes(base64.b64decode(image['data']))
            report.update(checks=checks,windows_count=window_count,planning_requests=len(creations()),javascript_exceptions=errors,
                basemap_requests_blocked=True,
                final_state=await js('({level:s.flightReplay.result.level,decision:s.flightReplay.result.decision,trajectory_id:s.flightReplay.result.trajectory_id,window_status:s.departureWindows.result?.window_job_status})'))
            assert not errors,errors
        except Exception as exc:
            report.update(error=str(exc),checks=checks,javascript_exceptions=errors)
            image=await call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
            (WORK/'v3-ui-failed.png').write_bytes(base64.b64decode(image['data']))
            (WORK/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            raise
        (WORK/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


asyncio.run(main())
