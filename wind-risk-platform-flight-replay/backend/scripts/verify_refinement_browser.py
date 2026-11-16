"""CDP acceptance with isolated 8002/5175 storage/weather, real search and UI."""
import asyncio
import base64
import json
from pathlib import Path
import time
import re
from urllib.request import build_opener, ProxyHandler, Request

import websockets

WORK = Path(__file__).resolve().parents[3] / "data/route_refinement_tests"
OPENER = build_opener(ProxyHandler({}))


async def main():
    tab = json.loads(OPENER.open(Request("http://127.0.0.1:9228/json/new?about:blank", method="PUT")).read())
    report = dict(checks=[], errors=[], requests=[], publications=[], owner_acks=0, cancellations=[],
                  scope="isolated synthetic weather/storage, real parallel planner, owner lease and UI")
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as socket:
        seq = 0
        paused = []
        async def call(method, params=None):
            nonlocal seq
            seq += 1
            await socket.send(json.dumps(dict(id=seq, method=method, params=params or {})))
            while True:
                event = json.loads(await socket.recv())
                if event.get("method") == "Runtime.exceptionThrown":
                    report["errors"].append(event["params"])
                if event.get("method") == "Network.requestWillBeSent":
                    req = event["params"]["request"]
                    if "/api/" in req["url"] and "/map-tiles/" not in req["url"]:
                        report["requests"].append({k: req[k] for k in ("url", "method", "postData") if k in req})
                if event.get('method') == 'Network.webSocketFrameSent':
                    try:
                        if json.loads(event['params']['response']['payloadData']).get('type') == 'ack':
                            report['owner_acks'] += 1
                    except (ValueError, KeyError):
                        pass
                if event.get('method') == 'Fetch.requestPaused':
                    paused.append(event['params'])
                if event.get("id") == seq:
                    if "error" in event:
                        raise RuntimeError(event["error"])
                    return event.get("result", {})
        async def js(code):
            value = await call("Runtime.evaluate", dict(expression=code, returnByValue=True, awaitPromise=True))
            if "exceptionDetails" in value:
                raise RuntimeError(value["exceptionDetails"])
            return value["result"].get("value")
        async def run(code):
            return await js("(async()=>{" + code + ";})()")
        async def check(code, label):
            assert await js("Boolean(" + code + ")"), label
            report["checks"].append(label)
        async def wait(code, label, seconds=45):
            end = time.monotonic() + seconds
            while not await js("Boolean(" + code + ")"):
                assert time.monotonic() < end, label + ": " + str(await js("document.body.innerText.slice(-1800)"))
                await asyncio.sleep(.15)
        async def screenshot(name):
            value = await call("Page.captureScreenshot", dict(format="png", captureBeyondViewport=False))
            (WORK / name).write_bytes(base64.b64decode(value["data"]))

        def job_state(identifier):
            return json.loads(OPENER.open('http://127.0.0.1:8002/api/route/plan/jobs/'+identifier, timeout=15).read())

        def latest_job():
            for request in reversed(report['requests']):
                found = re.search(r'/route/plan/jobs/([a-f0-9-]{36})', request['url'])
                if found:
                    return found[1]
            raise AssertionError('No polled job')

        async def stopped(identifier, cause, limit):
            began = time.monotonic()
            while True:
                state = job_state(identifier)
                if state['status']=='cancelled' and state['compute_finished']:
                    break
                assert time.monotonic()-began < limit, (cause,state)
                await asyncio.sleep(.05)
            report['cancellations'].append(dict(cause=cause,seconds=time.monotonic()-began,job_id=identifier,
                                                compute_finished=state['compute_finished'],owner_timeout_seconds=state['owner_timeout_seconds']))
            report['checks'].append(cause+' releases computation')

        async def prepare_plan():
            await run('s.clearRoute();s.selection.source="wrf"')
            await wait('!s.loading && s.options.source.startsWith("WRF")', 'Source settles')
            await run('s.selection.level="100m AGL";s.planner.startText="117.2,30.8";s.planner.endText="117.95,30.8";s.planner.departureMode="scheduled";s.planner.departureAt="2026-11-05T08:33";s.flightSettings.averageGroundSpeedMps=10')
            await asyncio.sleep(.5)
            await wait('!s.loading', 'Settings settle')

        async def new_plan():
            await prepare_plan()
            await run('void s.runPlan()')
            await wait('document.querySelector(".search-corridor")', 'New search corridor')
            return latest_job()

        async def reopen():
            await call('Page.navigate',dict(url='http://127.0.0.1:5175/'))
            await wait('document.querySelector("#app")?.__vue_app__?._instance?.setupState', 'Reloaded Vue')
            await run('window.s=document.querySelector("#app").__vue_app__._instance.setupState')
            await wait('!s.loading && s.options.cycles?.length', 'Reloaded weather')
        try:
            await call("Page.enable")
            await call("Runtime.enable")
            await call("Network.enable")
            await call('Page.addScriptToEvaluateOnNewDocument', {'source': '''
              window.__ownerTestSockets=[];
              const OriginalSocket=window.WebSocket;
              window.WebSocket=class extends OriginalSocket { constructor(...args){super(...args);window.__ownerTestSockets.push(this)} };
            '''})
            await call("Network.setBlockedURLs", dict(urls=["*/api/map-tiles/*"]))
            await call("Emulation.setDeviceMetricsOverride", dict(width=1920, height=1080, deviceScaleFactor=1, mobile=False))
            await call("Page.navigate", dict(url="http://127.0.0.1:5175/"))
            await wait('document.querySelector("#app")?.__vue_app__?._instance?.setupState', "Vue ready")
            await run('window.s=document.querySelector("#app").__vue_app__._instance.setupState')
            await wait("!s.loading && s.options.cycles?.length", "Weather ready")
            await run('s.clearRoute();s.selection.source="wrf"')
            await wait('!s.loading && s.options.source.startsWith("WRF")', "WRF ready")
            await run('s.selection.level="100m AGL";s.planner.algorithm="wa_lpa_star";s.planner.strategy="wind_avoidance";s.planner.startText="117.2,30.8";s.planner.endText="117.95,30.8";s.planner.departureMode="scheduled";s.planner.departureAt="2026-11-05T08:33";s.flightSettings.averageGroundSpeedMps=10;s.planner.name="分段过程验收";s.workspace.reset()')
            await asyncio.sleep(.5)
            began = time.monotonic()
            await run("window.windRef=s.wind;window.planStarts=0;void s.runPlan()")
            await wait('document.querySelectorAll(".search-corridor").length>0', "Candidates render before formal result")
            await check("s.searchProgress.forward.length<=3 && s.searchProgress.backward.length<=3", "Three display representatives per side")
            await wait('s.searchProgress.forward.length && s.searchProgress.backward.length', "Both search directions appear")
            await check('!s.planner.points.length && !s.fixedRouteSelection && !s.departureWindows.active', "Search geometry never becomes a saved or assessed official route")
            await check('!document.querySelector(`.leaflet-overlay-pane path[stroke="#00f3ff"]`)', "No premature blue straight reference")
            await check('[...document.querySelectorAll(".search-corridor")].every(el=>el.getAttribute("stroke-dasharray"))', "Candidate lines use thin dashes")
            await check('[...document.querySelectorAll(".search-corridor")].every(el=>el.getAttribute("stroke")==="#00f3ff" && Number(el.getAttribute("stroke-width"))===4)', 'Candidates use official blue and width')
            await check('[...document.querySelectorAll(".search-corridor-outline")].every(el=>Number(el.getAttribute("stroke-width"))===6 && el.getAttribute("stroke-dasharray")==="10 8")', 'Dark outline uses matching dashes')
            initial_job = latest_job()
            background = json.loads(OPENER.open(Request('http://127.0.0.1:9228/json/new?about:blank',method='PUT')).read())
            await call('Target.activateTarget',{'targetId':background['id']})
            await asyncio.sleep(17)
            await call('Target.activateTarget',{'targetId':tab['id']})
            assert job_state(initial_job)['status']=='running'
            report['checks'].append('Background tab stays active beyond isolated 15-second lease')
            await screenshot("search-corridors.png")
            await run('window.lastGeometry=null;window.visibleUpdates=[];window.track=setInterval(()=>{const key=JSON.stringify(s.planner.points);if(key!==lastGeometry&&s.planner.points.length){visibleUpdates.push({at:performance.now(),count:s.planner.points.length});lastGeometry=key}},50)')
            await wait("s.planner.points.length>=2", "Initial best publishes after 30 seconds")
            report["first_visible_seconds"] = time.monotonic() - began
            assert report["first_visible_seconds"] >= 29., report
            report["checks"].append("30-second initial publication cadence")
            await check('!document.querySelector(".search-corridor") && !s.searchProgress && s.planner.planning', "First official publication removes all candidate geometry")
            await wait("!s.planner.planning", "Final comparison completes", seconds=25)
            await check('!s.searchProgress && !document.querySelector(".search-corridor")', "Completion clears candidate geometry")
            await check('document.querySelector(`.leaflet-overlay-pane path[stroke="#00f3ff"]`)', "Official blue route remains")
            assert report['owner_acks'] >= 2
            report['checks'].append('Owner heartbeats traverse Vite WebSocket proxy')
            report['server_timings'] = {key:job_state(initial_job).get(key) for key in ['preparation_seconds','queue_wait_seconds','first_feasible_elapsed_seconds']}
            await run("clearInterval(track)")
            report["publications"] = await js("visibleUpdates")
            assert len(report["publications"]) <= 2
            report["checks"].append("No intermediate official route churn")
            await wait("s.departureWindows.result && s.flightReplay.result && !s.flightReplay.loading", "Windows and profile complete")
            await check("s.flightReplay.result.dataset_id===s.fixedRouteSelection.dataset_id", "Profile and map retain the pinned forecast version")
            await run("s.selectedRouteSampleIndex=Math.floor(s.displayAnalysis.samples.length/2)")
            await check('s.mapRef.$.setupState.routeSelectionGroup?.getLayers().length>0 || document.querySelector(".leaflet-routeSelection-pane circle")', "Chart point links to map highlight")
            await run("void s.persistRoute()")
            await wait("s.savedCurrent && !s.savingRoute", "Save succeeds in independent test database")
            await run("window.savedRoute=s.savedRoutes.find(r=>r.route_id===s.activeRouteId);void s.runPlan(savedRoute)")
            await wait("s.flightReplay.result && !s.flightReplay.loading", "Saved fixed route reloads")
            await check("s.fixedRouteSelection.route_id===savedRoute.route_id", "Reload selection uses route ID")
            await run("await s.replay.seek(60)")
            await check("s.flightReplay.seconds===60", "Replay remains functional")
            # Start another search and cancel while its process layer is live.
            await run('s.clearRoute();s.planner.startText="117.2,30.8";s.planner.endText="117.9,30.8"')
            await asyncio.sleep(.5)
            await wait('!s.loading', "Selection settles before next click")
            await run('void s.runPlan()')
            await wait('document.querySelector(".search-corridor")', "Second process layer appears")
            await run("s.stopPlanning()")
            await check('!s.searchProgress && !document.querySelector(".search-corridor")', "Cancel clears candidates immediately")
            await asyncio.sleep(1.2)
            await check('!s.searchProgress && !document.querySelector(".search-corridor") && !s.planner.points.length', "Late cancelled responses cannot restore candidates or a route")
            await run('void s.runPlan()')
            await wait('document.querySelector(".search-corridor")', "Process visible before parameter change")
            await run('s.selection.level="500m AGL"')
            await wait('!s.planner.planning && !s.loading', "Missing-data replacement task ends")
            await asyncio.sleep(1.)
            await check('!s.searchProgress && !s.planner.points.length && !document.querySelector(".search-corridor")', "Changed-height task isolates late old results and process responses")
            # A permanent owner-transport loss must cancel even while ordinary
            # status polling is still running. Production grace is 60s; this
            # isolated service uses 15s to keep acceptance practical.
            lost_job = await new_plan()
            await run('for(const socket of window.__ownerTestSockets){if(socket.url.includes("/api/route/plan/jobs/") && socket.readyState===1){socket.onclose=null;socket.close()}}')
            await stopped(lost_job,'Lost owner transport',18)
            await wait('!s.planner.planning', 'Lost-owner UI settles')
            await check('!s.searchProgress && !document.querySelector(".search-corridor")', 'Lease expiry clears candidates')
            # Ordinary refresh sends keepalive cancellation; its task must stop
            # before the 15-second lease fallback.
            refresh_job = await new_plan()
            await call('Page.reload')
            await stopped(refresh_job,'Page refresh',2.5)
            await wait('document.querySelector("#app")?.__vue_app__?._instance?.setupState', 'Refreshed app')
            await run('window.s=document.querySelector("#app").__vue_app__._instance.setupState')
            await wait('!s.loading && s.options.cycles?.length', 'Refresh data ready')
            # Hold the creation RESPONSE after admission, so the page has no ID
            # and cannot send an explicit cancellation for it.
            await prepare_plan()
            await call('Fetch.enable', {'patterns':[{'urlPattern':'*/api/route/plan/jobs','requestStage':'Response'}]})
            await run('void s.runPlan()')
            until=time.monotonic()+15
            while not paused:
                assert time.monotonic()<until
                await js('true')
                await asyncio.sleep(.05)
            held=paused[-1]
            body=await call('Fetch.getResponseBody',{'requestId':held['requestId']})
            raw=base64.b64decode(body['body']) if body.get('base64Encoded') else body['body']
            unknown_job=json.loads(raw)['job_id']
            await call('Page.navigate',{'url':'about:blank'})
            await call('Fetch.disable')
            await stopped(unknown_job,'Close before creation ID arrives',18)
            await reopen()
            close_job=await new_plan()
            await screenshot('owner-before-close.png')
            await call('Page.close')
            await stopped(close_job,'Actual tab close',2.5)
            assert not report["errors"], report["errors"]
            report["checks"].append("No browser runtime exceptions")
        finally:
            (WORK / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": len(report["checks"]), "first_visible_seconds": report.get("first_visible_seconds")}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
