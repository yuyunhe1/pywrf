"""Real WRF cache and replay checks; never save a route or start a planner."""
import asyncio
import base64
import json
import time
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener

import websockets

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'data/resource_wind_optimization_tests'
HTTP = build_opener(ProxyHandler({}))


async def main():
    tab = json.load(HTTP.open(Request('http://127.0.0.1:9231/json/new?about:blank', method='PUT')))
    report = dict(checks=[], errors=[], writes_platform_history=False, frame_requests=[])
    async with websockets.connect(tab['webSocketDebuggerUrl'], max_size=None) as socket:
        seq = 0

        async def call(method, params=None):
            nonlocal seq
            seq += 1
            await socket.send(json.dumps(dict(id=seq, method=method, params=params or {})))
            while True:
                event = json.loads(await socket.recv())
                if event.get('method') == 'Runtime.exceptionThrown':
                    report['errors'].append(event['params'])
                if event.get('method') == 'Network.requestWillBeSent':
                    url = event['params']['request']['url']
                    if any(part in url for part in ('/api/wind?', '/api/heatmap?', '/api/flight-replay/wind?')):
                        report['frame_requests'].append(url)
                if event.get('id') == seq:
                    if 'error' in event: raise RuntimeError(event['error'])
                    return event.get('result', {})

        async def js(code):
            value = await call('Runtime.evaluate', dict(expression=code, awaitPromise=True, returnByValue=True))
            if 'exceptionDetails' in value: raise RuntimeError(value['exceptionDetails'])
            return value['result'].get('value')

        async def run(code): return await js('(async()=>{' + code + '})()')

        async def wait(code, label, seconds=60):
            until = time.monotonic() + seconds
            while not await js('Boolean(' + code + ')'):
                if time.monotonic() >= until:
                    raise AssertionError((label, await js('document.body.innerText.slice(-1000)')))
                await asyncio.sleep(.1)

        async def check(code, label):
            assert await run('return Boolean(' + code + ')'), label
            report['checks'].append(label)

        await call('Page.enable')
        await call('Runtime.enable')
        await call('Network.enable')
        await call('Network.setBlockedURLs', {'urls': ['*/api/map-tiles/*']})
        await call('Emulation.setDeviceMetricsOverride', dict(width=1920, height=1080, deviceScaleFactor=1, mobile=False))
        await call('Page.navigate', {'url': 'http://127.0.0.1:5174/'})
        await wait('document.querySelector("#app")?.__vue_app__?._instance?.setupState', 'Vue startup')
        await run('window.s=document.querySelector("#app").__vue_app__._instance.setupState;window.beforeHistory=await fetch("/api/routes").then(r=>r.json());s.selection.source="wrf"')
        await wait('!s.loading && s.metadata?.wind_time_model==="NATIVE_WRF_TEN_MINUTE_HOLD_V1"', 'Real native WRF map')
        await run('window.api=await import("/src/api.js");window.frames=s.options.valid_times;window.a=frames.findIndex(t=>t.label===s.selection.validTime);window.aTime=frames[a].label;window.bTime=frames[a+1].label;s.layers.velocity=false;s.layers.heatmap=true')
        await run('s.selection.validTime=bTime;await Promise.resolve();window.coldB=performance.now();await s.loadWindField();window.coldB=performance.now()-coldB;window.heatCanvas=document.querySelector(".wind-speed-canvas-layer")')
        before_requests = len(report['frame_requests'])
        report['cached_switch_ms'] = await run('s.selection.validTime=aTime;await Promise.resolve();const start=performance.now();await s.loadWindField();return performance.now()-start')
        await check('s.metadata.valid_time===frames[a].valid_time', 'Cached snapshot keeps the selected exact native time')
        report['repeat_switch_network_requests'] = len(report['frame_requests']) - before_requests
        assert report['repeat_switch_network_requests'] == 0
        report['checks'].append('Returning to a cached snapshot sends zero weather HTTP requests')
        await check('document.querySelector(".wind-speed-canvas-layer")===heatCanvas', 'Heatmap updates reuse the canvas')
        report['first_uncached_switch_ms'] = await js('coldB')
        await run('s.layers.velocity=true;s.layers.heatmap=false;s.beginCustomDrawing();s.drawing.add([118.75397,30.39420]);s.drawing.add([119.13025,30.08811]);await s.drawing.complete({...s.selection})')
        await wait('s.flightReplay.result && !s.flightReplay.mapLoading', 'Real long unsaved route profile')
        await run('window.map=s.mapRef.$.setupState.map')
        await wait('Object.values(map._layers).some(layer=>layer.flightBandKey && layer._windy?.statistics.fieldReady)', 'Particle field built')
        await check('Object.values(map._layers).filter(layer=>layer.flightBandKey).length===1', 'All wind grades share exactly one particle field')
        report['particle_resources'] = await js('Object.values(map._layers).find(layer=>layer.flightBandKey)._windy.statistics')
        assert report['particle_resources']['particles'] <= 6000
        assert report['particle_resources']['fieldVectors'] <= 250000
        report['checks'].append('Particle and display-field budgets are enforced')
        await check('s.flightReplay.result.flight_duration_seconds>3600 && !s.activeRouteId', 'Long replay stays unsaved')
        await check('s.flightReplay.result.wind_time_model==="NATIVE_WRF_TEN_MINUTE_HOLD_V1"', 'Replay retains the native ten-minute model')
        await asyncio.sleep(1)
        await run('window.firstWind=s.flightReplay.mapWind;await s.replay.seek(900)')
        await check('s.flightReplay.mapWind===firstWind && s.flightReplay.mapUpdateIntervalSeconds===1800', 'Particles remain unchanged before thirty flight minutes')
        await wait('s.replay.cacheStats().active===0 && s.replay.cacheStats().queued===0', 'Two-frame prefetch settled')
        before_requests = len(report['frame_requests'])
        report['prefetched_replay_switch_ms'] = await run('const begin=performance.now();await s.replay.seek(1800);return performance.now()-begin')
        # Seeking starts the next speculative fetch; inspect only the requested
        # current frame to distinguish it from the next lookahead.
        current_at = await js('s.flightReplay.mapTime')
        await check('s.flightReplay.seconds===1800 && s.flightReplay.mapWind!==firstWind', 'At thirty minutes the real frame changes')
        await check('new Date(s.flightReplay.mapTime).getTime()===Math.floor((Date.parse(s.flightReplay.result.departure_at)+1800000)/600000)*600000', 'Background time is the exact preceding native frame')
        await check('(await api.getReplayWind(s.flightReplay.result.dataset_id,s.flightReplay.mapTime)).velocity.every((component,j)=>component.data.every((value,i)=>value===s.flightReplay.mapWind.velocity[j].data[i]))', 'Display optimization retains every original native U/V value')
        from urllib.parse import parse_qs, urlparse
        new_requests = report['frame_requests'][before_requests:]
        assert not any(parse_qs(urlparse(url).query).get('at') == [current_at] for url in new_requests)
        report['checks'].append('The needed playback frame is served from prefetch')
        await run('s.selectRouteSample(1)')
        await wait('document.querySelector(`.leaflet-routeSelection-pane path[stroke="#ff3b30"]`)', 'Red curve locator')
        report['checks'].append('Curve selection still highlights the route in red')
        await check('(()=>{const layer=Object.values(map._layers).find(layer=>layer.flightBandKey);return [1.5,3.3,5.4,7.9,8].every((speed,index)=>layer.options.colorIndex(speed)===index)})()', 'The five exact grade boundaries retain their original colours')
        await run('s.mapRef.$.setupState.toggleBlock(4)')
        await check('Object.values(map._layers).find(layer=>layer.flightBandKey).options.visibleBands.join() === "4"', 'Legend filtering works within the single field')
        await run('s.mapRef.$.setupState.toggleBlock(4)')
        report['page_cache'] = await js('api.getWindCacheStats()')
        report['replay_cache'] = await js('s.replay.cacheStats()')
        assert report['page_cache']['bytes'] <= 48 * 1024 ** 2
        assert report['replay_cache']['bytes'] <= 16 * 1024 ** 2
        assert report['page_cache']['active'] <= 2
        report['checks'].append('Cache byte budgets and global weather request concurrency hold')
        # Inject a transport delay into a separate controller using the REAL
        # profile/frames, leaving the backend and user's settings unchanged.
        await run('window.factory=(await import("/src/utils/flightReplay.js")).createFlightReplayController;window.testReplay=factory({requestProfile:async()=>s.flightReplay.result,requestFrame:async(...args)=>{if(Date.parse(args[1])>Date.parse(s.flightReplay.result.departure_at))await new Promise(r=>setTimeout(r,1500));return api.getReplayWind(...args)},onChange:state=>window.testState=state,prefetchFrames:0,mapUpdateIntervalSeconds:1800});await testReplay.setContext({route_id:"isolated-delay-test",points:s.planner.points},s.departureConfiguration(),{dataset_id:s.flightReplay.result.dataset_id,evaluated_at:s.flightReplay.result.departure_at});await testReplay.seek(1790);testReplay.setRate(60);await testReplay.play()')
        await asyncio.sleep(1.1)
        await check('testState.seconds>=1850 && testState.mapLoading && testState.playing', 'Aircraft ticks continue during an artificially slow weather switch')
        await check('Date.parse(testState.mapTime)<Date.parse(testState.result.departure_at)+1800000', 'Pending download does not relabel the displayed old background')
        await run('testReplay.dispose();s.replay.pause()')
        await check('JSON.stringify(beforeHistory)===JSON.stringify(await fetch("/api/routes").then(r=>r.json()))', 'Normal platform history is unchanged')
        await call('Emulation.setDeviceMetricsOverride', dict(width=1366, height=768, deviceScaleFactor=1, mobile=False))
        await asyncio.sleep(.3)
        image = await call('Page.captureScreenshot', {'format': 'png'})
        (WORK / 'browser.png').write_bytes(base64.b64decode(image['data']))
        assert not report['errors'], report['errors']
        report['checks'].append('No browser runtime exceptions')
        (WORK / 'browser.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        print(json.dumps({key: value for key, value in report.items() if key != 'frame_requests'}, ensure_ascii=False))
        await call('Page.close')


if __name__ == '__main__': asyncio.run(main())
