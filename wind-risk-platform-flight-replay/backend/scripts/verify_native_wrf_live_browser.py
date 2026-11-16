"""Read-only production browser audit against the newly synchronized real batch."""
import asyncio
import base64
import json
import os
from pathlib import Path
import time
from urllib.request import build_opener,ProxyHandler,Request

import websockets

ROOT=Path(__file__).resolve().parents[3]
WORK=ROOT/'data/native_wrf_10min_tests'
HTTP=build_opener(ProxyHandler({}))
MODEL='NATIVE_WRF_TEN_MINUTE_HOLD_V1'
BROWSER_URL=os.getenv('NATIVE_BROWSER_URL','http://127.0.0.1:5174/')
REPORT_STEM=os.getenv('NATIVE_BROWSER_REPORT_STEM','production-browser')


async def main():
    sample=json.loads((WORK/'real-performance.json').read_text(encoding='utf-8'))['short_routes'][0]
    tab=json.loads(HTTP.open(Request('http://127.0.0.1:9228/json/new?about:blank',method='PUT')).read())
    report=dict(checks=[],errors=[],writes_platform_history=False,browser_url=BROWSER_URL)
    async with websockets.connect(tab['webSocketDebuggerUrl'],max_size=None) as socket:
        sequence=0
        async def call(method,params=None):
            nonlocal sequence
            sequence+=1
            await socket.send(json.dumps(dict(id=sequence,method=method,params=params or {})))
            while True:
                event=json.loads(await socket.recv())
                if event.get('method')=='Runtime.exceptionThrown':report['errors'].append(event['params'])
                if event.get('id')==sequence:
                    if 'error' in event:raise RuntimeError(event['error'])
                    return event.get('result',{})
        async def js(expression):
            result=await call('Runtime.evaluate',dict(expression=expression,returnByValue=True,awaitPromise=True))
            if 'exceptionDetails' in result:raise RuntimeError(result['exceptionDetails'])
            return result['result'].get('value')
        async def run(code):return await js('(async()=>{'+code+'})()')
        async def wait(expression,label):
            end=time.monotonic()+60
            while not await js('Boolean('+expression+')'):
                assert time.monotonic()<end,(label,await js('({error:s?.error,windows:s?.departureWindows,replay:s?.flightReplay.error})'))
                await asyncio.sleep(.1)
        async def check(expression,label):
            assert await run('return Boolean('+expression+')'),label
            report['checks'].append(label)
        await call('Page.enable');await call('Runtime.enable')
        await call('Emulation.setDeviceMetricsOverride',dict(width=1920,height=1080,deviceScaleFactor=1,mobile=False))
        await call('Page.navigate',dict(url=BROWSER_URL))
        await wait('document.querySelector("#app")?.__vue_app__?._instance?.setupState','Vue startup')
        await run('window.s=document.querySelector("#app").__vue_app__._instance.setupState;window.historyBefore=await fetch("/api/routes").then(r=>r.json());s.selection.source="wrf"')
        await wait('!s.loading && s.options.wind_time_model==="'+MODEL+'"','Native WRF options')
        await check('s.options.valid_times.length===289 && new Set(s.options.valid_times.map(r=>r.forecast_minutes)).size===289','Real map exposes all 289 unique native times')
        await run('s.selection.level="100m AGL";s.layers.velocity=true')
        await wait('!s.loading && s.metadata?.wind_time_model==="'+MODEL+'"','Native map data')
        await check('s.options.lookahead.available && Number.isInteger(s.options.valid_times.find(r=>r.valid_time===s.options.lookahead.target_valid_time).forecast_minutes)','Three-hour target resolves to an existing native minute')
        points=[sample['start'],sample['end']]
        await run('s.beginCustomDrawing();'+''.join('s.drawing.add('+json.dumps(point)+');' for point in points)+'await s.drawing.complete({...s.selection})')
        await wait('s.departureWindows.result && s.flightReplay.result && !s.flightReplay.mapLoading','Unsaved real fixed-route assessment')
        await check('!s.activeRouteId && s.planner.points.length===2','Real drawing stays unsaved and preserves its endpoints')
        await check('s.departureWindows.result.wind_time_model===s.flightReplay.result.wind_time_model && s.flightReplay.result.wind_time_model==="'+MODEL+'"','Real windows and profile use one native model')
        await check('s.departureWindows.result.dataset_id===s.flightReplay.result.dataset_id && s.flightReplay.result.frame_times.length===289','Windows and curve pin the same complete real batch')
        if await js('Boolean(s.departureWindows.result.departure_windows.length)'):
            await check('Date.parse(s.flightReplay.result.departure_at)===Date.parse(s.departureWindows.result.departure_windows[0].start)','Real drawing defaults to its earliest confirmed window')
        await run('s.selectRouteSample(1)')
        await wait('document.querySelector(`.leaflet-routeSelection-pane path[stroke="#ff3b30"]`)','Red curve locator')
        await run('await s.replay.seek(5);window.originalBackground=s.flightReplay.mapWind;await s.replay.seek(15)')
        await check('s.flightReplay.seconds===15 && s.flightReplay.mapWind===originalBackground && s.flightReplay.mapUpdateIntervalSeconds===1800','Aircraft moves while the particle background remains reused')
        await check('s.flightReplay.mapWind.metadata.wind_time_model==="'+MODEL+'"','Real replay reads native wind rather than hourly interpolation')
        for width,height in ((1366,768),(1200,768),(390,844)):
            await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=height,deviceScaleFactor=1,mobile=False))
            await asyncio.sleep(.2)
            await check('document.documentElement.scrollWidth<=window.innerWidth+1',f'Native real-data view fits {width}px')
        await call('Emulation.setDeviceMetricsOverride',dict(width=1920,height=1080,deviceScaleFactor=1,mobile=False))
        await asyncio.sleep(2)
        screenshot=await call('Page.captureScreenshot',dict(format='png'))
        (WORK/(REPORT_STEM+'.png')).write_bytes(base64.b64decode(screenshot['data']))
        await check('JSON.stringify(historyBefore)===JSON.stringify(await fetch("/api/routes").then(r=>r.json()))','Production browser audit never writes route history')
        assert not report['errors'],report['errors']
        report['checks'].append('No browser runtime exceptions')
        (WORK/(REPORT_STEM+'.json')).write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))
        await call('Page.close')


if __name__=='__main__':asyncio.run(main())
