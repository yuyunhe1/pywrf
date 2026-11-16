"""Chrome DevTools acceptance against flight_replay_smoke_server.py (isolated data)."""
import asyncio
import base64
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np
import websockets

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / "data" / "flight_replay_tests"
opener = build_opener(ProxyHandler({}))


async def main():
    # A fresh target keeps DevTools' cached exceptions from earlier test pages
    # out of this run, while retaining every error from this page's full life.
    pages = json.loads(opener.open("http://127.0.0.1:9224/json/list").read())
    page = json.loads(opener.open(Request("http://127.0.0.1:9224/json/new?about:blank", method="PUT")).read())
    for old in pages:
        if old["type"] == "page":
            opener.open("http://127.0.0.1:9224/json/close/" + old["id"]).close()
    checks, exceptions, screenshots = [], [], []
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as socket:
        sequence = 0

        async def call(method, params=None):
            nonlocal sequence
            sequence += 1
            identifier = sequence
            await socket.send(json.dumps({"id": identifier, "method": method, "params": params or {}}))
            while True:
                message = json.loads(await socket.recv())
                if message.get("method") == "Runtime.exceptionThrown":
                    exceptions.append(message["params"]["exceptionDetails"])
                if message.get("id") == identifier:
                    if "error" in message:
                        raise RuntimeError(message["error"])
                    return message.get("result", {})

        async def js(expression):
            result = await call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
            if "exceptionDetails" in result:
                raise AssertionError(result["exceptionDetails"])
            return result["result"].get("value")

        async def wait(expression, label):
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if await js(expression):
                    checks.append(label)
                    print("[OK] " + label, flush=True)
                    return
                await asyncio.sleep(.15)
            raise AssertionError(label + "\n" + str(await js("document.body.innerText.slice(-7000)")))

        async def click_route(letter):
            await js("[...document.querySelectorAll('.saved-route-button')].find(e=>e.textContent.includes('回放验收 " + letter + "')).click()")

        async def input_threshold(label, value):
            await js("(() => {const e=[...document.querySelectorAll('.threshold-grid label')].find(e=>e.textContent.includes(" + json.dumps(label) + ")).querySelector('input'); e.value=" + json.dumps(str(value)) + "; e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()")

        async def screenshot(name):
            image = await call("Page.captureScreenshot", {"format": "png"})
            (WORK / name).write_bytes(base64.b64decode(image["data"]))
            screenshots.append(name)

        try:
            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride", {"width": 1600, "height": 1080, "deviceScaleFactor": 1, "mobile": False})
            # The datetime input must still mean Beijing time outside China.
            await call("Emulation.setTimezoneOverride", {"timezoneId": "America/New_York"})
            await call("Page.navigate", {"url": "http://127.0.0.1:5175"})
            await wait("document.querySelectorAll('.saved-route-button').length===2", "isolated saved routes load")
            await js("window.app=document.querySelector('#wind-map').__vueParentComponent.parent.setupState; window.mapState=document.querySelector('#wind-map').__vueParentComponent.setupState; window.panel=document.querySelector('.analysis-panel').__vueParentComponent.setupState")
            await click_route("A")
            await wait("app.flightReplay.result?.route_id==='replay-a' && app.flightReplay.mapWind && !app.loading", "saved route defaults to a timed profile and frozen WRF map")
            await wait("app.flightReplay.mode==='flight' && app.flightReplay.result.decision.status==='allowed' && document.querySelector('.decision-label').textContent.includes('所选起飞时间')", "whole-flight decision uses selected departure rather than map snapshot")
            await wait("document.querySelector('.flight-aircraft-icon') && app.flightReplay.result.segments.some(s=>document.querySelector('.leaflet-overlay-pane path[stroke=\"'+s.color+'\"]'))", "aircraft and risk-coloured route render on Leaflet")
            await wait("app.flightReplay.result.dataset_id===app.displayMetadata.dataset_id && app.flightReplay.result.level===app.displayMetadata.level && app.flightReplay.result.forecast_cycle===app.displayMetadata.cycle", "route, chart and map share one forecast revision and height")

            await wait("document.querySelectorAll('.departure-window-button').length>0", "departure windows are selectable")
            await js("document.querySelector('.departure-window-button').click()")
            await wait("app.flightReplay.result && app.flightReplay.result.departure_at===app.departureWindows.result.departure_windows[0].start && !app.flightReplay.mapLoading", "clicking a window sets departure time and recomputes the flight")
            window = json.loads(await js("JSON.stringify(app.departureWindows.result.departure_windows[0])"))
            custom = datetime.fromisoformat(window["start"].replace("Z", "+00:00")) + timedelta(minutes=5)
            custom_iso = custom.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            input_value = (custom + timedelta(hours=8)).replace(tzinfo=None).isoformat(timespec="seconds")
            await js("(() => {const e=document.querySelector('input[type=datetime-local]');e.value=" + json.dumps(input_value) + ";e.dispatchEvent(new Event('change',{bubbles:true}));})()")
            await wait("app.flightReplay.result?.departure_at===" + json.dumps(custom_iso), "custom departure input stays in Beijing time in a New York browser")

            await input_threshold("平均对地速度", 20)
            await wait("app.flightReplay.result?.average_ground_speed_mps===20 && app.flightReplay.result.departure_at===" + json.dumps(custom_iso), "speed edit recomputes ETAs while preserving selected departure")
            await input_threshold("四级风", 5.5)
            await wait("app.flightReplay.result?.decision.status==='blocked' && document.querySelector('.decision-head strong').textContent==='禁止通航'", "hard-limit edit updates whole-flight decision and route risk")
            await input_threshold("四级风", 7.9)
            await input_threshold("平均对地速度", 0)
            await wait("!app.flightReplay.result && !app.flightReplay.loading && document.querySelector('.decision-card').textContent.includes('速度')", "invalid speed clears old result and explains the input error")
            await input_threshold("平均对地速度", .05)
            await wait("app.flightReplay.result?.average_ground_speed_mps===0.05 && app.flightReplay.result.decision.status==='unknown'", "small positive speed is valid and long flight becomes data-insufficient")
            await input_threshold("平均对地速度", 10)
            await wait("app.flightReplay.result?.average_ground_speed_mps===10 && !app.flightReplay.mapLoading", "normal speed restores flight replay")

            await js("[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='风速热力图').click()")
            await wait("document.querySelector('.wind-speed-canvas-layer') && app.layers.heatmap", "single-time heatmap renders in replay mode")
            await js("(() => {const e=document.querySelector('[aria-label=飞行回放进度]');e.value=app.flightReplay.result.flight_duration_seconds/2;e.dispatchEvent(new Event('input',{bubbles:true}));})()")
            await wait("app.flightReplay.seconds>100 && app.flightReplay.mapTime===app.displayMetadata.valid_time && !app.flightReplay.mapLoading", "timeline seek commits aircraft and map at one playback time")
            await wait("Math.abs(mapState.flightProgressGroup.getLayers().at(-1).getLatLng().lng-app.aircraftPosition.lon)<1e-9", "rendered aircraft coordinates match the timed route")
            await screenshot("flight-replay-heatmap.png")

            # Click the actual ECharts canvas at an actual series coordinate.
            await js("panel.windChartEl.scrollIntoView({block:'center'})")
            await asyncio.sleep(.25)
            target = await js("window.chartTarget=(() => {window.targetIndex=Math.floor(app.displayAnalysis.samples.length*.4);const s=app.displayAnalysis.samples[targetIndex];const r=panel.windChartEl.querySelector('canvas').getBoundingClientRect();return {x:r.left+panel.windChart.convertToPixel({xAxisIndex:0},s.distance_km),y:r.top+panel.windChart.convertToPixel({yAxisIndex:0},s.wind_speed)};})()")
            await call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": target["x"], "y": target["y"], "button": "left", "clickCount": 1})
            await call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": target["x"], "y": target["y"], "button": "left", "clickCount": 1})
            await wait("app.selectedRouteSampleIndex!==null && Math.abs(app.flightReplay.seconds-app.displayAnalysis.samples[app.selectedRouteSampleIndex].elapsed_seconds)<.00001 && Math.abs(app.displayAnalysis.samples[app.selectedRouteSampleIndex].distance_km-app.displayAnalysis.samples[targetIndex].distance_km)<1 && !app.flightReplay.mapLoading", "actual chart click highlights nearest visible route point and seeks its ETA")
            await wait("mapState.routeSelectionGroup.getLayers().length===2 && Math.abs(mapState.routeSelectionGroup.getLayers()[1].getLatLng().lng-app.displayAnalysis.samples[app.selectedRouteSampleIndex].lon)<1e-9 && [...document.querySelectorAll('.leaflet-tooltip')].some(e=>e.textContent.includes('预计经过'))", "red map marker and tooltip show the selected point's time and risk")
            await screenshot("flight-replay-chart-selection.png")

            await js("document.querySelector('[aria-label=播放航程]').click()")
            previous = await js("app.flightReplay.seconds")
            await wait("app.flightReplay.playing && app.flightReplay.seconds>" + str(previous + 20), "play advances map time and aircraft")
            await js("document.querySelector('[aria-label=暂停回放]').click()")
            stopped = await js("app.flightReplay.seconds")
            await asyncio.sleep(.8)
            assert await js("app.flightReplay.seconds") == stopped
            checks.append("pause keeps the playback position fixed")
            await js("[...document.querySelectorAll('.replay-mode-switch button')].find(e=>e.textContent.trim()==='单时刻剖面').click()")
            await wait("app.flightReplay.mode==='snapshot' && !document.querySelector('.flight-aircraft-icon') && app.displayAnalysis===app.analysis", "snapshot switch restores static map and profile with explicit labels")
            await js("[...document.querySelectorAll('.replay-mode-switch button')].find(e=>e.textContent.trim()==='飞行过程剖面').click()")
            await wait("app.flightReplay.mode==='flight' && document.querySelector('.flight-aircraft-icon')", "flight switch restores the selected flight without changing geometry")
            await js("[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='风向粒子流').click()")
            await wait("app.layers.velocity && document.querySelector('canvas') && app.displayWind.velocity.length===2", "particle layer consumes the same timed vector snapshot")
            await wait("[...document.querySelectorAll('canvas.velocity-overlay')].some(c=>{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=3;i<d.length;i+=4)if(d[i]>0)return true;return false})", "particle animation paints visible wind trails")
            await js("window.layerBeforePlay=mapState.velocityLayers[0];document.querySelector('[aria-label=播放航程]').click()")
            start_particle = await js("app.flightReplay.seconds")
            await wait("app.flightReplay.seconds>" + str(start_particle + 20) + " && mapState.velocityLayers[0]===layerBeforePlay", "particle replay advances time by updating existing layers")
            await js("document.querySelector('[aria-label=暂停回放]').click()")

            old = await js("app.flightReplay.result.dataset_id")
            fixture = json.loads((WORK / "browser" / "fixture.json").read_text())
            file = Path(fixture["cache_dir"]) / "smoke" / "h3.npz"
            with np.load(file) as data:
                changed = {key: data[key] for key in data.files}
            changed["u"] = np.full_like(changed["u"], 6.1)
            np.savez_compressed(file, **changed)
            await js("document.querySelector('[aria-label=播放航程]').click();window.dispatchEvent(new Event('focus'))")
            await wait("app.flightReplay.result?.dataset_id!==" + json.dumps(old) + " && app.flightReplay.mapWind?.metadata.dataset_id===app.flightReplay.result?.dataset_id && !app.flightReplay.playing && app.flightReplay.notice.includes('已更新')", "new WRF data pauses playback and re-evaluates every view consistently")

            end = await js("app.flightReplay.result.frame_times.at(-1)")
            late = (datetime.fromisoformat(end.replace("Z", "+00:00")) - timedelta(minutes=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            late_input = (datetime.fromisoformat(late.replace("Z", "+00:00")) + timedelta(hours=8)).replace(tzinfo=None).isoformat(timespec="seconds")
            await js("(() => {const e=document.querySelector('input[type=datetime-local]');e.value=" + json.dumps(late_input) + ";e.dispatchEvent(new Event('change',{bubbles:true}));})()")
            await wait("app.flightReplay.result?.departure_at===" + json.dumps(late) + " && app.flightReplay.result.decision.status==='unknown' && app.flightReplay.result.segments.some(s=>s.status==='unknown')", "flight beyond forecast coverage is unknown and has grey segments")
            await js("(() => {const e=document.querySelector('[aria-label=飞行回放进度]');e.value=app.flightReplay.result.flight_duration_seconds;e.dispatchEvent(new Event('input',{bubbles:true}));})()")
            await wait("!app.flightReplay.mapLoading && !app.displayWind && !app.displayHeatmap && document.querySelector('.replay-notice')?.textContent.includes('数据不足')", "missing playback time clears old wind instead of extrapolating")
            await screenshot("flight-replay-missing-data.png")

            await click_route("A")
            await click_route("B")
            await wait("app.flightReplay.result?.route_id==='replay-b' && app.fixedRouteSelection.route_id==='replay-b' && !app.flightReplay.mapLoading", "rapid saved-route switching retains only the latest flight")
            await js("[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='清除航线').click()")
            await wait("!document.querySelector('.flight-replay-controls') && !document.querySelector('.flight-aircraft-icon') && !app.fixedRouteSelection", "clearing route removes replay, aircraft and future windows")
            assert not exceptions, json.dumps(exceptions, ensure_ascii=False)
            checks.append("no browser JavaScript exceptions")
            report = {"passed": True, "checks": checks, "javascript_exceptions": exceptions, "screenshots": screenshots}
        except Exception as exc:
            await screenshot("flight-replay-failure.png")
            report = {"passed": False, "checks": checks, "error": str(exc), "javascript_exceptions": exceptions,
                      "state": await js("JSON.stringify({body:document.body.innerText,mode:window.app?.flightReplay?.mode,error:window.app?.flightReplay?.error})")}
            (WORK / "browser_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        (WORK / "browser_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": True, "checks": len(checks)}), flush=True)


asyncio.run(main())
