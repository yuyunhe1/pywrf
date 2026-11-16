"""Real WA-LPA* user-case UI acceptance on isolated ports 8002/5175/9226."""
import asyncio
import base64
import json
from pathlib import Path
import time
import sys
import shutil
from urllib.request import build_opener, ProxyHandler, Request
import websockets

WORK = Path(__file__).resolve().parents[3] / "data/cross_time_avoidance_tests/browser"
OPENER = build_opener(ProxyHandler({}))


async def main():
    WORK.mkdir(parents=True, exist_ok=True)
    long_mode = '--v3-long' in sys.argv
    long_plan = None
    if long_mode:
        long_plan = json.loads((WORK.parent/'v3-user-case-first-wrf_v3.json').read_text(encoding='utf-8'))
        source = Path(__file__).resolve().parents[2]/'data/wrf_v3_runtime/trajectories'
        target = WORK/'runtime/trajectories'
        target.mkdir(parents=True,exist_ok=True)
        for suffix in ('.json','.npz'):
            shutil.copy2(source/(long_plan['trajectory_id']+suffix),target/(long_plan['trajectory_id']+suffix))
        saved = json.loads(OPENER.open('http://127.0.0.1:8002/api/routes').read())
        for row in saved:
            if row['name']=='V3 长航线避风验收':
                OPENER.open(Request('http://127.0.0.1:8002/api/routes/'+row['route_id'],method='DELETE')).close()
        payload={'name':'V3 长航线避风验收','points':long_plan['points'],'level':long_plan['level'],
                 'start':long_plan['points'][0][:2],'end':long_plan['points'][-1][:2],'v3':long_plan['v3']}
        OPENER.open(Request('http://127.0.0.1:8002/api/routes',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=30).close()
    page = json.loads(OPENER.open(Request("http://127.0.0.1:9226/json/new?about:blank", method="PUT")).read())
    checks, errors = [], []
    report = {}
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as socket:
        sequence = 0
        async def call(method, params=None):
            nonlocal sequence
            sequence += 1
            identifier = sequence
            await socket.send(json.dumps({"id": identifier, "method": method, "params": params or {}}))
            while True:
                event = json.loads(await socket.recv())
                if event.get("method") == "Runtime.exceptionThrown":
                    errors.append(event["params"])
                if event.get("id") == identifier:
                    if "error" in event:
                        raise AssertionError(event["error"])
                    return event.get("result", {})
        async def js(code):
            result = await call("Runtime.evaluate", {"expression": code, "returnByValue": True, "awaitPromise": True})
            if "exceptionDetails" in result:
                raise AssertionError(result["exceptionDetails"])
            return result["result"].get("value")
        async def wait(code, label, seconds=35):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                if await js("Boolean(" + code + ")"):
                    checks.append(label)
                    print("[OK] " + label, flush=True)
                    return
                await asyncio.sleep(.15)
            raise AssertionError(label + "\n" + str(await js("document.body.innerText.slice(-4500)")))
        async def click(text):
            await js("[...document.querySelectorAll('button')].find(e=>e.textContent.includes(" + json.dumps(text) + ")).click()")
        async def screenshot(name):
            value = await call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            (WORK / name).write_bytes(base64.b64decode(value["data"]))
        try:
            await call("Page.enable"); await call("Runtime.enable"); await call("Network.enable")
            await call("Network.setBlockedURLs", {"urls": ["*/api/map-tiles/*"]})
            await call("Emulation.setDeviceMetricsOverride", {"width": 1600, "height": 1050, "deviceScaleFactor": 1, "mobile": False})
            await call("Page.navigate", {"url": "http://127.0.0.1:5175/"})
            await wait("document.querySelector('#app')?.__vue_app__?._instance?.setupState?.options?.v3_native", "WRF options loaded")
            if long_mode:
                await js("window.s=document.querySelector('#app').__vue_app__._instance.setupState")
                await wait("s.savedRoutes.some(r=>r.name==='V3 长航线避风验收')",'Saved long V3 curve material is available')
                began=time.monotonic()
                await js("[...document.querySelectorAll('.saved-route-button')].find(e=>e.textContent.includes('V3 长航线避风验收')).click()")
                await wait("s.planner.points.length>9000",'Saved 152 km V3 geometry appears before retiming finishes',15)
                report['geometry_seconds']=time.monotonic()-began
                await wait("document.querySelector('[aria-label=\"起飞时间（北京时间）\"]')",'Fixed-path departure control is available')
                await js("(()=>{const e=document.querySelector('[aria-label=\"起飞时间（北京时间）\"]');e.value='2026-11-04T17:00:00';e.dispatchEvent(new Event('change',{bubbles:true}));})()")
                await wait("s.flightReplay.result?.departure_at==='2026-11-04T09:00:00.000Z' && !s.flightReplay.loading",'Long fixed curve retimes at the original 17:00 departure',120)
                assert await js('s.flightReplay.result.analysis.navigation_allowed'),await js('s.flightReplay.result.decision')
                assert await js('s.flightReplay.result.trajectory_id==='+json.dumps(long_plan['trajectory_id']))
                report.update(profile_seconds=time.monotonic()-began,profile_samples=await js('s.flightReplay.result.analysis.samples.length'),
                              distance_km=await js('s.flightReplay.result.analysis.total_distance_km'))
                await wait("document.querySelector('.leaflet-overlay-pane path[stroke=\"#00f3ff\"][stroke-width=\"4\"]')",'Long route retains the uniform blue line')
                await screenshot('v3-long-user-case.png')
                await js("[...document.querySelectorAll('button')].find(e=>e.textContent.includes('停止窗口计算'))?.click()")
                await wait('!s.departureWindows.loading','Long window task supports cancellation without losing the route',15)
                assert await js('s.flightReplay.result.analysis.navigation_allowed && s.planner.points.length>9000')
                assert not errors,errors
                report.update(checks=checks,javascript_exceptions=errors,basemap_requests_blocked=True)
                return
            await js("window.s=document.querySelector('#app').__vue_app__._instance.setupState;s.selection.source='wrf'")
            await wait("s.metadata?.source?.includes('WRF') && !s.loading", "WRF selection settled")
            await js("s.planner.algorithm='wa_lpa_star';s.selection.level='100m AGL';s.planner.startText='117.17468, 30.91165';s.planner.endText='118.13049, 30.39657';s.planner.strategy='wind_avoidance';s.planner.departureMode='scheduled';s.planner.departureAt='2026-11-04T17:00:00'")
            await asyncio.sleep(.5)
            began = time.monotonic()
            await click("生成规划航线")
            await wait("s.planner.points.length>2 && s.flightReplay.result?.analysis?.navigation_allowed", "Real user-case detour and full future-flight decision displayed", 20)
            report["browser_first_seconds"] = time.monotonic() - began
            assert await js("s.planner.planning && s.flightReplay.result.departure_at==='2026-11-04T09:00:00.000Z' && s.flightReplay.result.level==='100m AGL'")
            assert await js("s.planner.points.some(p=>Math.abs((p[1]-30.91165)-((30.39657-30.91165)/(118.13049-117.17468))*(p[0]-117.17468))>.02)")
            report["first_distance_km"] = await js("s.flightReplay.result.analysis.total_distance_km")
            await wait("!s.planner.planning", "Optimization finishes or times out while retaining a verified route", 50)
            assert await js("s.flightReplay.result.analysis.navigation_allowed && s.planner.points.length>2")
            report["final_distance_km"] = await js("s.flightReplay.result.analysis.total_distance_km")
            report["max_wind_speed"] = await js("s.flightReplay.result.analysis.max_wind_speed")
            report["published_revisions"] = await js("Number(s.fixedRouteSelection.planning_key.split(':').at(-1))")
            assert report["published_revisions"] >= 2
            await wait("document.querySelector('.leaflet-overlay-pane path[stroke=\"#00f3ff\"][stroke-width=\"4\"]')", "Route uses the uniform old-blue thick line")
            await wait("!s.flightReplay.mapLoading && s.flightReplay.mapWind?.metadata?.dataset_id===s.flightReplay.result.dataset_id", "Map and route use the same WRF dataset", 45)
            await js("document.querySelector('[_echarts_instance_]').scrollIntoView({block:'center'})")
            point = await js("(async()=>{const e=document.querySelector('[_echarts_instance_]');const c=(await import('/src/utils/charts.js')).getInstanceByDom(e);const i=Math.floor(s.displayAnalysis.samples.length/2),q=s.displayAnalysis.samples[i],xy=c.convertToPixel({gridIndex:0},[q.distance_km,q.wind_speed]),r=e.getBoundingClientRect();return {x:r.x+xy[0],y:r.y+xy[1]};})()")
            for kind in ("mousePressed", "mouseReleased"):
                await call("Input.dispatchMouseEvent", {"type": kind, **point, "button": "left", "clickCount": 1})
            await wait("s.selectedRouteSampleIndex!==null && document.querySelector('.leaflet-routeSelection-pane path[stroke=\"#ff3b30\"]')", "Chart click highlights the actual route position in red")
            await screenshot("wa-user-case.png")
            await js("s.planner.departureAt='2026-11-05T17:00:00'")
            await wait("!s.planner.planning && s.planner.points.length===0 && s.planner.planningMessage.includes('缺少')", "Out-of-forecast request returns an explicit missing-data state", 30)
            assert await js("!s.fixedRouteSelection && !document.querySelector('.leaflet-overlay-pane path[stroke=\"#00f3ff\"]') && [...document.querySelectorAll('button')].find(e=>e.textContent.includes('下载保存')).disabled")
            checks.append("Missing data draws no reference route and disables saving")
            await screenshot("no-fabricated-reference.png")
            assert not errors, errors
            report.update(checks=checks, javascript_exceptions=errors, basemap_requests_blocked=True,
                          scope="real WRF, route search, map, chart and missing-data UI; isolated test database")
        except Exception as exc:
            report.update(error=str(exc), checks=checks, javascript_exceptions=errors)
            await screenshot("avoidance-failed.png")
            raise
        finally:
            (WORK / ("v3-long-browser-report.json" if long_mode else "avoidance-browser-report.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OPENER.open("http://127.0.0.1:9226/json/close/" + page["id"]).close()


asyncio.run(main())
