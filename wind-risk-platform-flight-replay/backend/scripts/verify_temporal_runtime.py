"""Verify the running 5174/8001 platform with real WRF, without saving routes."""
import asyncio
import base64
import json
from pathlib import Path
from time import monotonic
from urllib.request import build_opener, ProxyHandler, Request

import websockets

WORK = Path(__file__).resolve().parents[3] / 'data/temporal_route_tests'
opener = build_opener(ProxyHandler({}))


async def main():
    plan = json.loads((WORK / 'live-plan.json').read_text(encoding='utf-8'))
    page = json.loads(opener.open(Request('http://127.0.0.1:9224/json/new?about:blank', method='PUT')).read())
    checks, errors, payloads = [], [], []
    async with websockets.connect(page['webSocketDebuggerUrl'], max_size=None) as socket:
        sequence = 0
        async def call(method, params=None):
            nonlocal sequence
            sequence += 1
            identifier = sequence
            await socket.send(json.dumps({'id': identifier, 'method': method, 'params': params or {}}))
            while True:
                event = json.loads(await socket.recv())
                if event.get('method') == 'Runtime.exceptionThrown':
                    errors.append(event['params'])
                if event.get('method') == 'Network.requestWillBeSent':
                    req = event['params']['request']
                    if '/api/route/plan/jobs' in req['url'] and req['method'] == 'POST':
                        payloads.append(json.loads(req['postData']))
                if event.get('id') == identifier:
                    return event.get('result', {})
        async def js(source):
            result = await call('Runtime.evaluate', {'expression': source, 'returnByValue': True, 'awaitPromise': True})
            assert 'exceptionDetails' not in result, result
            return result['result'].get('value')
        async def wait(source, label):
            deadline = monotonic() + 40
            while monotonic() < deadline:
                if await js('Boolean((' + source + '))'):
                    checks.append(label)
                    print('[OK] ' + label, flush=True)
                    return
                await asyncio.sleep(.15)
            raise AssertionError(label)
        await call('Runtime.enable')
        await call('Network.enable')
        await call('Page.enable')
        await call('Emulation.setDeviceMetricsOverride', {'width': 1600, 'height': 1080, 'deviceScaleFactor': 1, 'mobile': False})
        await call('Page.navigate', {'url': 'http://127.0.0.1:5174'})
        await wait("document.querySelector('#wind-map') && document.querySelectorAll('.saved-route-button').length>0", 'running platform loads actual saved routes')
        await js("window.app=document.querySelector('#wind-map').__vueParentComponent.parent.setupState")
        await wait('!app.loading', 'existing wind source finishes loading')
        count = await js('app.savedRoutes.length')
        start, end = plan['points'][0][:2], plan['points'][-1][:2]
        await js('app.planner.startText=' + json.dumps(','.join(map(str, start))) + ';app.planner.endText=' + json.dumps(','.join(map(str, end))))
        await js("[...document.querySelectorAll('button')].find(e=>e.textContent.includes('生成规划航线')).click()")
        await wait('app.fixedRouteSelection?.ephemeral && app.flightReplay.result && app.flightReplay.mapWind && app.departureWindows.result', 'actual new planning uses live hourly WRF and enters flight replay')
        assert payloads[-1]['time_aware'] and payloads[-1]['source'] == 'wrf' and payloads[-1]['average_ground_speed_mps'] == 10
        assert await js('app.flightReplay.result.dataset_id===app.displayMetadata.dataset_id && app.flightReplay.result.dataset_id===app.departureWindows.result.dataset_id && app.fixedRouteSelection.route_id===app.departureWindows.result.route_id')
        checks.append('running backend, windows, curve and map share the planned geometry and WRF revision')
        mismatch = await js("(() => {const s=app.flightReplay.result.analysis.samples[0],w=app.displayWind,[u,v]=w.velocity,h=u.header;let near=(x,a,dx,n)=>{let k=0;for(let i=1;i<n;i++)if(Math.abs(a+i*dx-x)<Math.abs(a+k*dx-x))k=i;return k};let i=near(s.lat,h.la1,-h.dy,h.ny)*h.nx+near(s.lon,h.lo1,h.dx,h.nx);return Math.abs(Math.hypot(u.data[i],v.data[i])-s.wind_speed)})()")
        assert mismatch < .00001
        checks.append('displayed real wind agrees with the planned passage-time sample')
        previous = await js('app.fixedRouteSelection.planning_key')
        await js("(() => {let e=[...document.querySelectorAll('.threshold-grid label')].find(e=>e.textContent.includes('平均对地速度')).querySelector('input');e.value='12';e.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await wait('app.flightReplay.result?.average_ground_speed_mps===12 && app.fixedRouteSelection.planning_key!==' + json.dumps(previous) + ' && !app.flightReplay.mapLoading', 'actual speed input automatically launches a new WRF route search')
        assert payloads[-1]['average_ground_speed_mps'] == 12
        assert await js('app.savedRoutes.length') == count
        assert not errors, errors
        checks.append('runtime verification creates no saved route and raises no JavaScript exceptions')
        image = await call('Page.captureScreenshot', {'format': 'png'})
        (WORK / 'runtime-platform.png').write_bytes(base64.b64decode(image['data']))
        summary = await js("({decision:app.flightReplay.result.decision.status,dataset_id:app.flightReplay.result.dataset_id,cycle:app.flightReplay.result.forecast_cycle,distance_km:app.flightReplay.result.analysis.total_distance_km,departure_at:app.flightReplay.result.departure_at,arrival_at:app.flightReplay.result.arrival_at})")
        report = {'passed': True, 'checks': checks, 'planning_requests': payloads, 'first_sample_wind_error': mismatch, **summary}
        (WORK / 'runtime-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        await js('app.clearRoute()')
        await asyncio.sleep(.5)
        print(json.dumps({'passed': True, 'checks': len(checks)}), flush=True)


asyncio.run(main())
