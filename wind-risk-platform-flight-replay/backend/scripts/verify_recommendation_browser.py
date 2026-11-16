"""Browser acceptance of real workflows over isolated weather and route storage."""
import asyncio
import base64
import json
import os
from pathlib import Path
import time
from urllib.request import build_opener,ProxyHandler,Request
import websockets

WORK=Path(os.getenv('RECOMMENDATION_BROWSER_REPORT_DIR',str(Path(__file__).resolve().parents[3]/'data/recommendation_tests')))
WORK.mkdir(parents=True,exist_ok=True)
HTTP=build_opener(ProxyHandler({}))


async def main():
    tab=json.loads(HTTP.open(Request('http://127.0.0.1:9228/json/new?about:blank',method='PUT')).read())
    report=dict(checks=[],errors=[],requests=[],scope='isolated fixture weather, real processes/workflows/windows/storage/UI')
    async with websockets.connect(tab['webSocketDebuggerUrl'],max_size=None) as socket:
        sequence=0
        async def call(method,params=None):
            nonlocal sequence
            sequence+=1
            await socket.send(json.dumps(dict(id=sequence,method=method,params=params or {})))
            while True:
                event=json.loads(await socket.recv())
                if event.get('method')=='Runtime.exceptionThrown': report['errors'].append(event['params'])
                if event.get('method')=='Network.requestWillBeSent':
                    req=event['params']['request']
                    if '/api/' in req['url'] and '/map-tiles/' not in req['url']:
                        report['requests'].append({k:req[k] for k in ['url','method','postData'] if k in req})
                if event.get('id')==sequence:
                    if 'error' in event: raise RuntimeError(event['error'])
                    return event.get('result',{})
        async def js(text):
            value=await call('Runtime.evaluate',dict(expression=text,returnByValue=True,awaitPromise=True))
            if 'exceptionDetails' in value: raise RuntimeError(value['exceptionDetails'])
            return value['result'].get('value')
        async def run(text): return await js('(async()=>{'+text+'})()')
        async def wait(expr,label,seconds=45):
            end=time.monotonic()+seconds
            while not await js('Boolean('+expr+')'):
                if time.monotonic()>end:
                    raise AssertionError(label+': '+str(await js('({text:document.body.innerText.slice(-3000),workflow:s.recommendation,err:s.error})')))
                await asyncio.sleep(.1)
        async def check(expr,label):
            assert await js('Boolean('+expr+')'),label
            report['checks'].append(label)
        async def screenshot(name):
            value=await call('Page.captureScreenshot',dict(format='png',captureBeyondViewport=False))
            (WORK/name).write_bytes(base64.b64decode(value['data']))
        await call('Page.enable');await call('Runtime.enable');await call('Network.enable')
        await call('Page.addScriptToEvaluateOnNewDocument',dict(source='Date.now=()=>1762302600000;'))
        await call('Emulation.setDeviceMetricsOverride',dict(width=1920,height=1080,deviceScaleFactor=1,mobile=False))
        await call('Page.navigate',dict(url='http://127.0.0.1:5175/'))
        await wait('document.querySelector("#app")?.__vue_app__?._instance?.setupState','Vue ready')
        await run('window.s=document.querySelector("#app").__vue_app__._instance.setupState')
        await wait('!s.loading && s.options.cycles?.length','weather ready')
        await check('document.body.innerText.includes("指定时间规划航线") && document.body.innerText.includes("推荐时间规划航线")','Both planning modules render')
        await check('document.querySelector(".route-planning-section [aria-label=\\"规划起飞方式\\"]") && !document.querySelector(".flight-settings-section [aria-label=\\"规划起飞方式\\"]")','Departure selection belongs to specified module')
        await run('s.selection.source="wrf"')
        await wait('!s.loading && s.options.source.startsWith("WRF")','WRF ready')
        if os.getenv('EXPECTED_WRF_TIME_MODEL'):
            expected=json.dumps(os.environ['EXPECTED_WRF_TIME_MODEL'])
            await check(f's.options.wind_time_model==={expected} && s.options.output_interval_seconds===600','WRF options identify native ten-minute output')
            await check('s.options.valid_times.some(t=>Number(t.forecast_hour)%1!==0) && s.options.valid_times.every(t=>t.forecast_minutes===Math.round(t.forecast_hour*60))','Minute selections retain their exact fractional hours')
            await run('window.nativeSamples=await Promise.all(["2026-11-05T00:30:00Z","2026-11-05T00:33:00Z","2026-11-05T00:40:00Z"].map(at=>fetch(`/api/flight-replay/wind?dataset_id=native-ui-100&at=${encodeURIComponent(at)}`).then(r=>r.json())))')
            await check('JSON.stringify(nativeSamples[0].velocity)===JSON.stringify(nativeSamples[1].velocity) && JSON.stringify(nativeSamples[0].velocity)!==JSON.stringify(nativeSamples[2].velocity)','Browser API receives unchanged native wind inside a frame and switches at the next frame')
            await check(f'nativeSamples.every(v=>v.metadata.wind_time_model==={expected})','Map wind carries the native time model identity')
        await run('s.selection.level="200m AGL";s.planner.startText="117.2,30.8";s.planner.endText="117.9,30.8";s.planner.departureMode="now";s.flightSettings.averageGroundSpeedMps=10')
        await asyncio.sleep(.6)
        await run('void s.runPlan()')
        await wait('s.recommendation?.recommendations?.length','Future departure recommendation')
        await wait('!s.planner.planning','Future workflow completes')
        await check('!s.planner.points.length && s.planner.departureMode==="now"','Future results do not adopt route or change requested time')
        await check('s.recommendation.attempts.some(a=>a.initial && a.status==="unconfirmed")','Initial attempt stops at isolated shortened first-solution budget')
        await check('s.recommendation.attempts.filter(a=>!a.initial && a.started_monotonic).length>=2','Future departure attempts are scheduled')
        await check('document.querySelectorAll(".recommendation-route").length && [...document.querySelectorAll(".recommendation-route")].every(p=>p.getAttribute("stroke-dasharray")==="10 8")','Validated future routes render as separate dashed previews')
        await run('window.future=s.recommendation.recommendations[0];await s.previewRecommendation(future)')
        await check('s.recommendationPreviewProfile && !s.planner.points.length','Preview shows risk profile without committing geometry')
        await check('!document.querySelector(`.leaflet-overlay-pane path[stroke="#00f3ff"]:not([stroke-dasharray])`)','Preview cannot become a blue official route')
        await run('await s.adoptRecommendation(future)')
        await wait('s.planner.points.length>=2 && s.flightReplay.result','Manual adoption loads a verified flight profile')
        await check('s.planner.departureMode==="scheduled" && s.activeRouteId===""','Adoption chooses the departure but does not save')

        await run('s.clearRoute();s.selection.level="100m AGL"')
        await asyncio.sleep(.6)
        await run('window.savedBefore=s.savedRoutes.length;s.beginCustomDrawing();s.drawing.rename("推荐界面独立验收");s.drawing.add([117.2,30.8]);s.drawing.add([117.9,30.8]);s.drawing.move(1,[117.9,30.8])')
        await check('document.body.innerText.includes("完成绘制并评估")','Drawing toolbar offers evaluation instead of saving')
        await run('await s.drawing.complete({...s.selection})')
        await wait('s.departureWindows.result && s.flightReplay.result','Drawn baseline windows and curve')
        await check('s.savedRoutes.length===savedBefore && !s.activeRouteId && s.recommendationBaseline.points.length===2','Completed drawing stays unsaved and preserves anchors')
        await check('s.flightReplay.result.departure_at===s.departureWindows.result.departure_windows[0].start && Date.parse(s.flightReplay.mapTime)===Math.floor(Date.parse(s.departureWindows.result.departure_windows[0].start)/600000)*600000','Drawing defaults profile and background to earliest confirmed window')
        await check('s.flightReplay.result.dataset_id===s.departureWindows.result.dataset_id && s.displayWind.metadata.dataset_id===s.departureWindows.result.dataset_id','Earliest-window weather and route verdict use one dataset')
        await run('window.manualDeparture=s.departureWindows.result.evaluated_at;await s.replay.chooseDeparture(manualDeparture);await s.departureEvaluator.refresh()')
        await check('s.flightReplay.departureAt===manualDeparture','Automatic window refresh preserves explicit departure selection')
        await run('window.base=JSON.stringify(s.planner.points);void s.runDetourRecommendation()')
        await wait('s.recommendation?.recommendations?.length','Beneficial detour recommendation',seconds=55)
        await wait('!s.recommending','Detour workflow completes',seconds=55)
        await check('JSON.stringify(s.planner.points)===base','Detour suggestions preserve original baseline')
        await check('s.recommendation.recommendations.every(c=>c.metrics.benefit_confirmed && c.metrics.improves && c.plan.distance_km<=s.departureWindows.result.route_distance_km*1.5+1e-6)','Recommendations have confirmed benefit and respect 50 percent bound')
        await run('window.detour=s.recommendation.recommendations[0];await s.previewRecommendation(detour)')
        report['detour_benefit']=await js('detour.metrics')
        report['detour_departure']=await js('detour.first_departure_at')
        await check('JSON.stringify(s.planner.points)===base && s.recommendationPreviewProfile','Detour preview leaves original path intact')
        # The fixture clock freezes Date.now; use a nonanimated map view for evidence.
        await run('window.testMap=document.querySelector("#wind-map").__vueParentComponent.setupState.map;testMap.stop();testMap.fitBounds(detour.plan.points.map(p=>[p[1],p[0]]),{paddingTopLeft:[350,150],paddingBottomRight:[390,150],animate:false})')
        await asyncio.sleep(.3)
        await screenshot('detour-preview.png')
        await run('await s.adoptRecommendation(detour)')
        await wait('!s.adoptingRecommendation && !s.recommendationPreview && s.flightReplay.result','Detour adopted')
        await check('s.savedRoutes.length===savedBefore && !s.activeRouteId','Adopted detour remains unsaved')
        await run('await s.persistRoute()')
        await wait('s.savedRoutes.length===savedBefore+1 && s.savedCurrent','Explicit save creates one independent record')
        await check('s.planner.points[0][0]===117.2 && s.planner.points.at(-1)[0]===117.9','Adoption and save keep locked endpoints')
        await run('await s.loadFixedRoute(s.savedCurrent.record || s.savedRoutes.find(r=>r.route_id===s.activeRouteId))')
        await wait('s.flightReplay.result && s.departureWindows.result','Saved route reload')
        if os.getenv('EXPECTED_WRF_TIME_MODEL'):
            await check(f's.flightReplay.result.wind_time_model==={expected} && s.departureWindows.result.wind_time_model==={expected}','Curve, fixed windows and replay share the native model')
            await check('s.savedRoutes.find(r=>r.route_id===s.activeRouteId).forecast_minutes===Math.round(s.savedRoutes.find(r=>r.route_id===s.activeRouteId).forecast_hour*60)','Saved route reload preserves exact forecast minutes')
        report['saved_route_id']=await js('s.activeRouteId')
        await run('s.selectRouteSample(1)')
        await wait('document.querySelector(`.leaflet-routeSelection-pane path[stroke="#ff3b30"]`)','Curve selection highlights route in red')
        report['checks'].append('Curve red location survives adoption and reload')
        await run('await s.replay.seek(600)')
        await check('s.flightReplay.seconds===600 && s.flightReplay.mapUpdateIntervalSeconds===1800','Playback retains 30-minute particle update interval')
        await run('window.gfsBase=JSON.stringify(s.planner.points);s.selection.source="gfs"')
        await wait('!s.loading && s.recommendationBaseline?.source==="gfs" && s.departureWindows.result?.source==="gfs"','GFS baseline re-evaluation')
        await check('s.flightSettings.averageGroundSpeedMps===50 && JSON.stringify(s.planner.points)===gfsBase','GFS defaults to 50 m/s and preserves fixed geometry')
        await run('s.clearRoute();s.planner.startText="117.2,30.8";s.planner.endText="117.9,30.8"')
        await asyncio.sleep(.3)
        await run('void s.runPlan()')
        await wait('!s.planner.planning && s.planner.points.length>=2 && s.flightReplay.result?.source==="gfs"','GFS specified-time planning')
        await check('s.flightReplay.result.average_ground_speed_mps===50 && s.flightReplay.result.analysis.navigation_allowed','GFS planning uses its own verified weather and 50 m/s model')
        await run('s.beginCustomDrawing();s.drawing.add([117.2,30.8]);s.drawing.add([117.3,30.8]);s.drawing.undo();s.drawing.add([117.4,30.8]);s.drawing.clear();s.drawing.cancel()')
        await check('!s.customDrawing.active && s.savedRoutes.length===savedBefore+1','Undo, clear and cancel never save a drawing')
        await run('s.selection.source="wrf"')
        await wait('!s.loading && s.options.source.startsWith("WRF")','WRF source restored')
        await run('s.selection.level="200m AGL";s.planner.startText="117.2,30.8";s.planner.endText="117.9,30.8"')
        await asyncio.sleep(.4)
        await run('void s.runPlan()')
        await wait('s.planner.planning && s.recommendation?.mode==="specified_time"','Owned workflow started for cancellation')
        cancel_id=await js('s.recommendationJobId')
        assert cancel_id,'Workflow owner ID missing'
        cancelled_at=time.monotonic()
        await run('s.stopPlanning()')
        await wait('!s.planner.planning','Cancel removes searching state')
        end=time.monotonic()+3
        while True:
            state=await js(f'fetch("/api/route/recommendation/jobs/{cancel_id}").then(r=>r.json())')
            if state.get('compute_finished'): break
            assert time.monotonic()<end,state
            await asyncio.sleep(.05)
        assert state['status']=='cancelled',state
        report['cancel_compute_release_seconds']=time.monotonic()-cancelled_at
        report['checks'].append('Cancel stops the owned workflow and releases computation')
        for width,height in [(1366,768),(1200,768),(390,844)]:
            await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=height,deviceScaleFactor=1,mobile=False))
            await asyncio.sleep(.2)
            await check('document.documentElement.scrollWidth<=window.innerWidth+1',f'No page overflow at {width}px')
        await call('Emulation.setDeviceMetricsOverride',dict(width=1920,height=1080,deviceScaleFactor=1,mobile=False))
        await check('!document.querySelector(".route-recommendation-section").classList.contains("collapsed")','Recommended module remains available')
        assert not report['errors'],report['errors']
        report['checks'].append('No browser runtime exceptions')
        (WORK/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k!='requests'},ensure_ascii=False))
        await call('Page.close')


if __name__=='__main__': asyncio.run(main())
