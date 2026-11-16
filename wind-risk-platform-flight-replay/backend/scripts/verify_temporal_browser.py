"""Actual UI acceptance for unsaved time-aware routes (isolated smoke server)."""
import asyncio
import base64
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

import websockets

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'data/temporal_route_tests'
opener = build_opener(ProxyHandler({}))
sys.stdout.reconfigure(encoding='utf-8')


async def main():
    pages = json.loads(opener.open('http://127.0.0.1:9224/json/list').read())
    page = json.loads(opener.open(Request('http://127.0.0.1:9224/json/new?about:blank', method='PUT')).read())
    for old in pages:
        if old['type'] == 'page':
            opener.open('http://127.0.0.1:9224/json/close/' + old['id']).close()
    checks, errors, requests = [], [], []
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
                    request = event['params']['request']
                    if '/api/route/plan/jobs' in request['url']:
                        requests.append(request)
                if event.get('id') == identifier:
                    return event.get('result', {})

        async def js(source):
            result = await call('Runtime.evaluate', {'expression': source, 'returnByValue': True, 'awaitPromise': True})
            if 'exceptionDetails' in result:
                raise AssertionError(result['exceptionDetails'])
            return result['result'].get('value')

        async def wait(source, label, seconds=45):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if await js('Boolean((' + source + '))'):
                    checks.append(label)
                    print('[OK] ' + label, flush=True)
                    return
                await asyncio.sleep(.15)
            raise AssertionError(label + '\n' + str(await js('document.body.innerText.slice(-6500)')))

        async def speed(value):
            await js("(() => {const e=[...document.querySelectorAll('.threshold-grid label')].find(e=>e.textContent.includes('平均对地速度')).querySelector('input');e.value=" + json.dumps(str(value)) + ";e.dispatchEvent(new Event('input',{bubbles:true}));})()")

        def creations():
            return [entry for entry in requests if entry['method'] == 'POST']

        async def screenshot(name):
            result = await call('Page.captureScreenshot', {'format': 'png'})
            (WORK / name).write_bytes(base64.b64decode(result['data']))

        try:
            await call('Runtime.enable')
            await call('Network.enable')
            await call('Page.enable')
            await call('Emulation.setDeviceMetricsOverride', {'width': 1600, 'height': 1080, 'deviceScaleFactor': 1, 'mobile': False})
            await call('Emulation.setTimezoneOverride', {'timezoneId': 'America/New_York'})
            await call('Page.navigate', {'url': 'http://127.0.0.1:5175'})
            await wait("document.querySelectorAll('.saved-route-button').length===2", 'isolated platform loads')
            await js("window.app=document.querySelector('#wind-map').__vueParentComponent.parent.setupState;window.mapState=document.querySelector('#wind-map').__vueParentComponent.setupState;window.panel=document.querySelector('.analysis-panel').__vueParentComponent.setupState")
            assert await js("app.planner.departureMode==='now' && app.flightSettings.averageGroundSpeedMps===10")
            checks.append('new planning defaults to now and 10 m/s')
            await js("app.planner.startText='118.45,31.1';app.planner.endText='119.4,31.1'")
            await js("[...document.querySelectorAll('button')].find(e=>e.textContent.includes('生成规划航线')).click()")
            await wait("app.fixedRouteSelection?.ephemeral && app.flightReplay.result && app.flightReplay.mapWind && !app.flightReplay.mapLoading", 'new plan enters timed decision, windows, risk map and replay without saving')
            payload = json.loads(creations()[-1]['postData'])
            assert payload['time_aware'] and payload['source'] == 'wrf' and payload['average_ground_speed_mps'] == 10
            assert payload.get('departure_at') is None
            checks.append('actual planning request sends temporal mode and resolves now on server')
            await wait("app.flightReplay.result.dataset_id===app.departureWindows.result.dataset_id && app.flightReplay.result.dataset_id===app.displayMetadata.dataset_id && app.flightReplay.result.decision.status==='allowed'", 'planning, windows, curve and map share frozen WRF revision')
            assert await js("app.savedRoutes.length===2 && app.flightReplay.result.flight_duration_seconds>7200 && document.querySelector('.flight-aircraft-icon')")
            checks.append('long route crosses hours, renders aircraft and does not create a saved route')
            old_key, old_jobs = await js('app.fixedRouteSelection.planning_key'), len(creations())
            await speed(20)
            await wait("app.flightReplay.result?.average_ground_speed_mps===20 && app.fixedRouteSelection.planning_key!==" + json.dumps(old_key), 'speed change automatically creates a new plan and replaces the old context')
            assert len(creations()) > old_jobs
            checks.append('speed edit performs a route search, beyond profile-only reassessment')

            cycle = datetime.fromisoformat(json.loads((ROOT / 'data/flight_replay_tests/browser/fixture.json').read_text())['cycle'])
            departure = cycle + timedelta(hours=4, minutes=5)
            departure_iso = departure.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            departure_input = (departure + timedelta(hours=8)).replace(tzinfo=None).isoformat(timespec='seconds')
            old_jobs = len(creations())
            await js("(() => {const e=document.querySelector('[aria-label=\"起飞时间（北京时间）\"]');e.value=" + json.dumps(departure_input) + ";e.dispatchEvent(new Event('change',{bubbles:true}));})()")
            await wait("app.flightReplay.result?.departure_at===" + json.dumps(departure_iso) + " && !app.flightReplay.mapLoading", 'future departure automatically replans and retains Beijing meaning in New York browser')
            assert len(creations()) > old_jobs
            assert await js("app.planner.departureMode==='scheduled' && app.flightReplay.result.departure_at!==app.departureWindows.result.evaluated_at")
            checks.append('selected departure remains independent of current-departure decision and snapshot time')
            await wait("document.querySelector('.departure-window-button')", 'new geometry provides selectable future windows')
            window = await js('app.departureWindows.result.departure_windows[0].start')
            old_jobs = len(creations())
            await js("document.querySelector('.departure-window-button').click()")
            await wait("app.flightReplay.result?.departure_at===" + json.dumps(window), 'selecting a future window replans at its start time and refreshes geometry windows')
            assert len(creations()) > old_jobs

            await speed(0)
            await wait("!app.flightReplay.result && !app.planner.planning && document.querySelector('.decision-head strong').textContent==='待评估'", 'invalid speed removes the old green decision and route')
            await speed(10)
            await wait("app.flightReplay.result?.average_ground_speed_mps===10 && app.flightReplay.mapWind", 'valid speed resumes automatic planning')
            await js("window.latestDeparture=app.flightReplay.result.departure_at")
            # Several same-tick edits must launch only the final settings.
            count = len(creations())
            await js('app.flightSettings.averageGroundSpeedMps=12;app.flightSettings.averageGroundSpeedMps=14;app.flightSettings.averageGroundSpeedMps=18')
            await wait("app.flightReplay.result?.average_ground_speed_mps===18 && app.flightReplay.result.departure_at===latestDeparture", 'rapid parameter edits commit only the newest speed and selected departure')
            assert len(creations()) == count + 1
            checks.append('automatic planning debounces rapid edits into one request')

            await js("(() => {const e=document.querySelector('[aria-label=飞行回放进度]');e.value=app.flightReplay.result.flight_duration_seconds*.4;e.dispatchEvent(new Event('input',{bubbles:true}));})()")
            await wait("app.flightReplay.seconds>100 && !app.flightReplay.mapLoading && app.displayWind.metadata.valid_time===app.flightReplay.mapTime && app.displayHeatmap.metadata.valid_time===app.flightReplay.mapTime", 'new-route seeking synchronizes aircraft, particles and heatmap at ETA')
            offset = await js('app.flightReplay.seconds')
            await wait('!app.planner.planning', 'background optimization finishes while retaining the feasible route')
            assert abs(await js('app.flightReplay.seconds') - offset) < .001
            checks.append('an unchanged final result preserves playback position')
            await js("panel.windChartEl.scrollIntoView({block:'center'})")
            await asyncio.sleep(.25)
            point = await js("(() => {const samples=app.displayAnalysis.samples;window.pickIndex=Math.floor(samples.length*.6);const s=samples[pickIndex],r=panel.windChartEl.querySelector('canvas').getBoundingClientRect();return {x:r.left+panel.windChart.convertToPixel({xAxisIndex:0},s.distance_km),y:r.top+panel.windChart.convertToPixel({yAxisIndex:0},s.wind_speed)};})()")
            for event in ('mouseMoved', 'mousePressed', 'mouseReleased'):
                await call('Input.dispatchMouseEvent', {'type': event, **point, 'button': 'left', 'clickCount': 1})
            print('[CLICK] ' + str(await js("JSON.stringify({point:" + json.dumps(point) + ",selected:app.selectedRouteSampleIndex,seconds:app.flightReplay.seconds,layers:mapState.routeSelectionGroup.getLayers().length})")), flush=True)
            await wait("app.selectedRouteSampleIndex!==null && mapState.routeSelectionGroup.getLayers().length===2 && Math.abs(app.flightReplay.seconds-app.displayAnalysis.samples[app.selectedRouteSampleIndex].elapsed_seconds)<.001 && !app.flightReplay.mapLoading", 'actual curve click highlights the new-route position red and seeks its passage time')
            await screenshot('new-route-flight.png')

            count = len(creations())
            await js("[...document.querySelectorAll('.replay-mode-switch button')].find(e=>e.textContent.trim()==='单时刻剖面').click()")
            await wait("app.flightReplay.mode==='snapshot' && app.displayAnalysis===app.analysis && !app.loading && !document.querySelector('.flight-aircraft-icon')", 'snapshot view explicitly uses the selected single-time field')
            assert len(creations()) == count
            checks.append('viewing a snapshot does not change planning departure or start a new search')
            await screenshot('new-route-snapshot.png')
            await js("[...document.querySelectorAll('.replay-mode-switch button')].find(e=>e.textContent.trim()==='飞行过程剖面').click()")
            await wait("app.flightMode && document.querySelector('.flight-aircraft-icon')", 'returning to flight view restores timed risk and aircraft')

            end = await js('app.flightReplay.result.frame_times.at(-1)')
            late = datetime.fromisoformat(end.replace('Z', '+00:00')) - timedelta(minutes=5)
            late_input = (late + timedelta(hours=8)).replace(tzinfo=None).isoformat(timespec='seconds')
            await js("(() => {const e=document.querySelector('[aria-label=\"起飞时间（北京时间）\"]');e.value=" + json.dumps(late_input) + ";e.dispatchEvent(new Event('change',{bubbles:true}));})()")
            await wait("app.flightReplay.result?.decision.status==='unknown' && app.flightReplay.result.segments.some(s=>s.status==='unknown') && app.planner.planningMessage.includes('参考线')", 'insufficient forecast cannot produce a flyable route and marks reference geometry clearly')
            await js("app.replay.seek(app.flightReplay.result.flight_duration_seconds)")
            await wait("!app.flightReplay.mapLoading && !app.displayWind && !app.displayHeatmap", 'late-arrival replay clears unavailable wind instead of extrapolating')
            await screenshot('new-route-missing.png')

            await js("[...document.querySelectorAll('.saved-route-button')].find(e=>e.textContent.includes('回放验收 B')).click()")
            await wait("app.flightReplay.result?.route_id==='replay-b' && !app.temporalPlanningEnabled", 'saved routes retain their fixed-geometry decision workflow')
            count = len(creations())
            await speed(15)
            await wait("app.flightReplay.result?.route_id==='replay-b' && app.flightReplay.result.average_ground_speed_mps===15", 'saved-route speed changes recompute flight assessment on saved geometry')
            assert len(creations()) == count
            assert not errors, errors
            checks.append('no browser JavaScript exceptions')
            report = {'passed': True, 'checks': checks, 'planning_requests': [json.loads(r['postData']) for r in creations()],
                      'cancelled_jobs': len([r for r in requests if r['method'] == 'DELETE']), 'javascript_exceptions': errors}
        except Exception as exc:
            await screenshot('new-route-failure.png')
            report = {'passed': False, 'checks': checks, 'error': str(exc), 'javascript_exceptions': errors,
                      'state': await js("JSON.stringify({body:document.body.innerText,planner:app?.planner,replay:app?.flightReplay?.error})")}
            (WORK / 'browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            raise
        (WORK / 'browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'passed': True, 'checks': len(checks)}), flush=True)


asyncio.run(main())
