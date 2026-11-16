"""CDP acceptance for custom fixed routes (isolated fixture service 8002/5175).

Start custom_routes_smoke_server.py and Chrome --remote-debugging-port=9227.
Weather fixtures exercise the actual continuous evaluators, not mocked verdicts.
"""
import asyncio
import base64
import json
from pathlib import Path
import time
from urllib.request import build_opener, ProxyHandler, Request

import websockets

WORK = Path(__file__).resolve().parents[3] / 'data/custom_route_tests'
OPENER = build_opener(ProxyHandler({}))


async def main():
    WORK.mkdir(parents=True, exist_ok=True)
    availability = json.loads(OPENER.open('http://127.0.0.1:8002/api/times?source=gfs').read())
    assert availability['source'].endswith('UI acceptance fixture'), 'Use only the isolated fixture service'
    for row in json.loads(OPENER.open('http://127.0.0.1:8002/api/routes').read()):
        OPENER.open(Request('http://127.0.0.1:8002/api/routes/' + row['route_id'], method='DELETE')).close()
    for row in json.loads(OPENER.open('http://127.0.0.1:8002/api/exported-routes').read()):
        from urllib.parse import quote
        OPENER.open(Request('http://127.0.0.1:8002/api/exported-routes/' + quote(row['file_name']), method='DELETE')).close()
    tab = json.loads(OPENER.open(Request('http://127.0.0.1:9227/json/new?about:blank', method='PUT')).read())
    report = {'checks': [], 'errors': [], 'requests': [], 'downloads': [],
              'scope': 'Deterministic hourly GFS/WRF fixtures; real save/export/windows/profile; tiles blocked'}
    async with websockets.connect(tab['webSocketDebuggerUrl'], max_size=None) as socket:
        sequence = 0

        async def call(method, params=None):
            nonlocal sequence
            sequence += 1
            await socket.send(json.dumps({'id': sequence, 'method': method, 'params': params or {}}))
            while True:
                event = json.loads(await socket.recv())
                if event.get('method') == 'Runtime.exceptionThrown':
                    report['errors'].append(event['params'])
                if event.get('method') == 'Network.requestWillBeSent':
                    request = event['params']['request']
                    if '/api/' in request['url'] and '/api/map-tiles/' not in request['url']:
                        report['requests'].append({key: request[key] for key in ('url', 'method', 'postData') if key in request})
                if event.get('method') == 'Page.downloadWillBegin':
                    report['downloads'].append(event['params'])
                if event.get('id') == sequence:
                    if 'error' in event:
                        raise RuntimeError(event['error'])
                    return event.get('result', {})

        async def js(code):
            result = await call('Runtime.evaluate', {'expression': code, 'returnByValue': True, 'awaitPromise': True})
            if 'exceptionDetails' in result:
                raise RuntimeError(result['exceptionDetails'])
            return result['result'].get('value')

        async def run(code):
            return await js('(async()=>{' + code + ';})()')

        async def check(code, label):
            assert await js('Boolean(' + code + ')'), label
            report['checks'].append(label)

        async def wait(code, label, seconds=20):
            end = time.monotonic() + seconds
            while not await js('Boolean(' + code + ')'):
                if time.monotonic() > end:
                    raise AssertionError(label + ': ' + str(await js('document.body.innerText.slice(-1800)')))
                await asyncio.sleep(.1)
            report['checks'].append(label)

        async def click_xy(x, y):
            for kind in ('mousePressed', 'mouseReleased'):
                await call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
            await asyncio.sleep(.1)

        async def click(expression):
            await run(f'({expression}).scrollIntoView({{block:"center"}})')
            await asyncio.sleep(.1)
            point = await js(f'(()=>{{const r=({expression}).getBoundingClientRect();return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()')
            await click_xy(**point)

        def button(text, root='document'):
            return f'[...{root}.querySelectorAll("button")].find(b=>b.textContent.trim()==={json.dumps(text)})'

        async def begin():
            await click(button('更多...'))
            await wait('document.querySelector(".el-dialog__body")', 'History dialog opens')
            await click(button('自定义绘制航线', 'document.querySelector(".el-dialog__body")'))
            await wait('s.customDrawing.active && !document.querySelector(".el-dialog__body")', 'Drawing starts and closes history dialog')
            await run('window.m=s.mapRef.$.setupState.map;m.setView([30.75,117.6],10,{animate:false})')
            await asyncio.sleep(.2)

        async def add(lon, lat):
            point = await js(f'(()=>{{const p=m.latLngToContainerPoint([{lat},{lon}]);const r=m.getContainer().getBoundingClientRect();return {{x:r.x+p.x,y:r.y+p.y}};}})()')
            await click_xy(**point)

        async def save_and_evaluate(name):
            await run(f'const input=document.querySelector("[aria-label=自定义航线名称]");input.value={json.dumps(name)};input.dispatchEvent(new Event("input",{{bubbles:true}}))')
            await click(button('保存固定航线', 'document.querySelector(".custom-route-controls")'))
            await wait('!s.customDrawing.active && s.fixedRouteSelection?.name===' + json.dumps(name), 'Saved route is automatically selected: ' + name)
            await wait('!s.departureWindows.loading && s.departureWindows.result && !s.flightReplay.loading && s.flightReplay.result', 'Current decision and windows finish: ' + name)
            return await js('s.fixedRouteSelection.route_id')

        async def screenshot(name):
            result = await call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
            (WORK / name).write_bytes(base64.b64decode(result['data']))

        try:
            await call('Page.enable')
            await call('Runtime.enable')
            await call('Network.enable')
            await call('Network.setBlockedURLs', {'urls': ['*/api/map-tiles/*']})
            await call('Emulation.setDeviceMetricsOverride', {'width': 1600, 'height': 1050, 'deviceScaleFactor': 1, 'mobile': False})
            await call('Page.navigate', {'url': 'http://127.0.0.1:5175/'})
            await call('Page.bringToFront')
            await wait("document.querySelector('#app')?.__vue_app__?._instance?.setupState?.options.valid_times.length", 'Fixture weather choices load')
            await run("window.s=document.querySelector('#app').__vue_app__._instance.setupState")
            await wait('!s.loading', 'Initial map loads')
            await click(button('更多...'))
            await wait('document.querySelector(".el-dialog__body")', 'History dialog is visible')
            await check('(()=>{const b=[...document.querySelector(".el-dialog__body").querySelectorAll("button")];return b[0].textContent.trim()==="自定义绘制航线" && b[1].textContent.includes("导入航线文件") && b[0].className===b[1].className;})()', 'Drawing entry sits immediately left of matching import button')
            await click(button('自定义绘制航线', 'document.querySelector(".el-dialog__body")'))
            await wait('s.customDrawing.active && !document.querySelector(".el-dialog__body")', 'Drawing entry activates draft')
            await run('window.m=s.mapRef.$.setupState.map;m.setView([30.75,117.6],10,{animate:false})')
            await check('!s.planner.planning && !s.fixedRouteSelection && !s.flightReplay.playing && !document.querySelector(".leaflet-draw") && !document.querySelector(".leaflet-popup")', 'Drawing clears old route/replay/popup and disables legacy draw tools')
            await check('document.querySelector("[aria-label=自定义航线名称]").value.match(/^自定义航线_\\d{8}_\\d{6}$/) && [...document.querySelector(".custom-route-controls").querySelectorAll("button")].at(-1).disabled', 'Default name and disabled empty save appear in bottom toolbar')
            await add(117.25, 30.9)
            await check('s.customDrawing.points.length===1 && [...document.querySelector(".custom-route-controls").querySelectorAll("button")].at(-1).disabled', 'One point cannot be saved')
            await add(117.6, 30.65)
            await add(117.95, 30.9)
            await check('s.customDrawing.points.length===3 && [...document.querySelectorAll(".custom-route-waypoint")].map(e=>e.textContent).join(",")==="1,2,3" && document.querySelector(".custom-route-preview").getAttribute("stroke")==="#00f3ff" && document.querySelector(".custom-route-preview").getAttribute("stroke-width")==="4"', 'Consecutive clicks create numbered points and thick blue ordered preview')
            await run('window.original=JSON.stringify(s.customDrawing.points)')
            drag_start = await js('(()=>{const r=document.querySelectorAll(".custom-route-waypoint")[1].getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()')
            await call('Input.dispatchMouseEvent', {'type': 'mouseMoved', **drag_start})
            await call('Input.dispatchMouseEvent', {'type': 'mousePressed', **drag_start, 'button': 'left', 'clickCount': 1})
            for step in range(1, 7):
                await call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': drag_start['x'] + step * 7, 'y': drag_start['y'] - step * 6, 'buttons': 1})
            await call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': drag_start['x'] + 42, 'y': drag_start['y'] - 36, 'button': 'left', 'clickCount': 1})
            await check('s.customDrawing.points.length===3 && JSON.stringify(s.customDrawing.points[0])===JSON.stringify(JSON.parse(original)[0]) && JSON.stringify(s.customDrawing.points[2])===JSON.stringify(JSON.parse(original)[2]) && JSON.stringify(s.customDrawing.points[1])!==JSON.stringify(JSON.parse(original)[1])', 'Dragging moves only the selected waypoint and never appends one')
            await call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': 750, 'y': 350, 'button': 'left', 'clickCount': 1})
            await call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 820, 'y': 400, 'buttons': 1})
            await call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': 820, 'y': 400, 'button': 'left', 'clickCount': 1})
            await run('m.setZoom(14,{animate:false})')
            await check('s.customDrawing.points.length===3 && s.selection.level==="100m AGL"', 'Map dragging/zoom add no points and zoom does not auto-select average height')
            await click(button('撤销上一点', 'document.querySelector(".custom-route-controls")'))
            await check('s.customDrawing.points.length===2 && document.querySelectorAll(".custom-route-waypoint").length===2', 'Undo removes the most recently added point and renumbers preview')
            await click(button('清空', 'document.querySelector(".custom-route-controls")'))
            await check('s.customDrawing.points.length===0 && !document.querySelector(".custom-route-waypoint")', 'Clear removes all draft geometry')
            await run('m.setView([30.75,117.6],10,{animate:false})')
            await add(117.25, 30.9)
            await add(117.95, 30.65)
            await click(button('取消', 'document.querySelector(".custom-route-controls")'))
            await check('!s.customDrawing.active && !s.customDrawing.points.length && document.querySelector(".leaflet-draw")', 'Cancel removes draft and restores normal map tools')
            assert not any(r['method'] == 'POST' and ('/api/route' in r['url']) for r in report['requests']), 'Drafting must not query routes or persist'
            report['checks'].append('Clicking, dragging and cancelling send no route analysis/search/save request')

            await begin()
            await add(117.25, 30.9)
            await add(117.6, 30.65)
            await add(117.95, 30.9)
            await run('window.draftBeforeSourceChange=JSON.stringify(s.customDrawing.points);s.selection.source="wrf"')
            await wait('s.selection.source==="wrf" && !s.loading && s.options.source.startsWith("WRF")', 'WRF wind preview loads during drafting')
            await run('s.selection.level="200m AGL";s.flightSettings.averageGroundSpeedMps=25;await s.loadWindField()')
            await check('JSON.stringify(s.customDrawing.points)===draftBeforeSourceChange && s.customDrawing.active && s.flightSettings.averageGroundSpeedMps===25', 'Source/height preview keeps geometry and accepts custom ground speed')
            await screenshot('drawing-toolbar.png')

            await call('Network.setBlockedURLs', {'urls': ['*/api/map-tiles/*', '*/api/routes']})
            await click(button('保存固定航线', 'document.querySelector(".custom-route-controls")'))
            await wait('s.customDrawing.error && !s.customDrawing.saving', 'Failed save reports an error')
            await check('s.customDrawing.active && JSON.stringify(s.customDrawing.points)===draftBeforeSourceChange', 'Failed save preserves the editable draft')
            await call('Network.setBlockedURLs', {'urls': ['*/api/map-tiles/*']})
            await run('const originalOpen=XMLHttpRequest.prototype.open, originalSend=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.open=function(method,url,...rest){this.customRouteMethod=method.toUpperCase();this.customRouteUrl=url;return originalOpen.call(this,method,url,...rest)};XMLHttpRequest.prototype.send=function(body){if(window.deferSave && this.customRouteMethod==="POST" && this.customRouteUrl==="/api/routes"){window.deferSave=false;window.releaseSave=()=>originalSend.call(this,body);return}return originalSend.call(this,body)};window.deferSave=true')
            await click(button('保存固定航线', 'document.querySelector(".custom-route-controls")'))
            await wait('s.customDrawing.saving && typeof releaseSave==="function"', 'Save enters busy state')
            await check('[...document.querySelector(".custom-route-controls").querySelectorAll("input,button")].every(e=>e.disabled) && document.querySelector(".control-panel").inert && !s.mapRef.$.setupState.draftMarkers.some(marker=>marker.dragging.enabled())', 'Pending save locks all draft actions, selections and waypoint dragging')
            await add(117.7, 30.8)
            await check('JSON.stringify(s.customDrawing.points)===draftBeforeSourceChange', 'Map clicks cannot modify a saving draft')
            await run('releaseSave()')
            await wait('!s.customDrawing.active && s.fixedRouteSelection && s.departureWindows.result && s.flightReplay.result && !s.loading', 'Retried save selects the persisted fixed route and evaluates it')
            wrf_id = await js('s.fixedRouteSelection.route_id')
            report['wrf_route_id'] = wrf_id
            await check('s.fixedRouteSelection.source==="wrf" && s.fixedRouteSelection.level==="200m AGL" && JSON.stringify(s.fixedRouteSelection.points)===draftBeforeSourceChange && s.flightReplay.result.average_ground_speed_mps===25', 'WRF route stores current source, height and exact waypoint order; evaluation uses 25 m/s')
            await check('s.departureWindows.result.current_departure.status==="blocked" && s.departureWindows.result.departure_windows.length>0 && s.flightReplay.result.decision.status==="blocked"', 'Cross-hour dangerous route is saved and future safe windows still appear')
            await check('s.savedRoutes.some(r=>r.route_id===s.fixedRouteSelection.route_id) && s.controlPanelRef.$.setupState.jsonFiles.some(r=>r.route_id===s.fixedRouteSelection.route_id)', 'Save refreshes both sidebar history and exported-history data')
            await check('document.querySelector(".departure-window-list li") && !document.querySelector(".departure-window-list button")', 'Future windows retain static green display')
            await screenshot('saved-wrf-windows.png')
            await run('window.savedWRF=s.fixedRouteSelection;window.pinned=s.flightReplay.mapWind;window.frameTime=s.flightReplay.mapTime;await s.replay.seek(60)')
            await check('s.flightReplay.seconds===60 && document.querySelector(".flight-aircraft-icon") && s.flightReplay.mapWind===pinned && s.flightReplay.mapTime===frameTime', 'Saved route replay moves the aircraft without rebuilding particle wind before 30 minutes')
            await run('const c=await import("/src/utils/charts.js");window.chart=c.getInstanceByDom(document.querySelector(".analysis-panel .chart"));document.querySelector(".analysis-panel .chart").scrollIntoView({block:"center"})')
            chart_point = await js('(()=>{const el=document.querySelector(".analysis-panel .chart"),r=el.getBoundingClientRect(),p=chart.convertToPixel({seriesIndex:0},[s.flightReplay.result.analysis.total_distance_km/2,5]);return {x:r.x+p[0],y:r.y+p[1]};})()')
            await click_xy(**chart_point)
            await check('s.selectedRouteSampleIndex!==null && document.querySelector(".leaflet-routeSelection-pane path")', 'Curve click still places a red route position marker')

            await begin()
            await check('!s.flightReplay.playing && !s.flightReplay.active && !s.departureWindows.active && !document.querySelector(".leaflet-routeSelection-pane path")', 'New drawing clears replay, decisions and old red selection')
            await run('s.selection.source="gfs"')
            await wait('s.selection.source==="gfs" && !s.loading && s.flightSettings.averageGroundSpeedMps===50', 'GFS preview restores default 50 m/s')
            await run('s.selection.level="100m AGL"')
            await add(117.45, 30.8)
            await add(117.6, 30.75)
            await add(117.75, 30.85)
            gfs_id = await save_and_evaluate('同名固定航线')
            report['gfs_route_id'] = gfs_id
            await check('s.fixedRouteSelection.source==="gfs" && s.fixedRouteSelection.level==="100m AGL" && s.flightReplay.result.average_ground_speed_mps===50 && s.departureWindows.result.current_departure.status==="allowed"', 'GFS route saves and evaluates with GFS at default 50 m/s')
            await run('window.frozenGeometry=JSON.stringify(s.fixedRouteSelection.points);s.flightSettings.averageGroundSpeedMps=40;Object.assign(s.thresholds,{safe:.4,notice:.8,warning:1.2,danger:1.9})')
            await wait('!s.departureWindows.loading && s.departureWindows.result?.wind_speed_limit_mps===1.9 && s.departureWindows.result.average_ground_speed_mps===40 && s.flightReplay.result?.average_ground_speed_mps===40', 'Speed and threshold changes trigger new fixed-path evaluations')
            await check('JSON.stringify(s.fixedRouteSelection.points)===frozenGeometry && s.departureWindows.result.current_departure.status==="blocked" && !s.planner.planning', 'Changed constraints update the verdict without searching or altering saved geometry')
            await run('s.flightSettings.averageGroundSpeedMps=50;Object.assign(s.thresholds,{safe:1.5,notice:3.3,warning:5.4,danger:7.9})')
            await wait('!s.departureWindows.loading && s.departureWindows.result?.wind_speed_limit_mps===7.9 && s.departureWindows.result.current_departure.status==="allowed"', 'Restoring thresholds restores the fixed-path safe windows')

            await begin()
            await run('s.selection.level="300m AGL"')
            await add(117.5, 30.8)
            await add(117.7, 30.7)
            no_window_id = await save_and_evaluate('同名固定航线')
            await check('s.departureWindows.result.current_departure.status==="blocked" && s.departureWindows.result.departure_windows.length===0 && s.departureWindows.result.empty_reason==="conditions_unsuitable" && document.body.innerText.includes("已覆盖的未来航程均触发")', 'Routes without any future safe window remain saved with an explicit reason')
            assert no_window_id != gfs_id
            await click(button('更多...'))
            await wait('s.controlPanelRef.$.setupState.jsonFiles.filter(r=>r.route_name==="同名固定航线").length===2', 'Same-name history contains both saved IDs')
            await run('const p=s.controlPanelRef.$.setupState;window.rowIndex=p.jsonFiles.findIndex(r=>r.route_id===' + json.dumps(gfs_id) + ')')
            await click('document.querySelectorAll(".el-table__body-wrapper tbody tr")[rowIndex].querySelector(".el-button--success")')
            await wait('s.fixedRouteSelection?.route_id===' + json.dumps(gfs_id) + ' && s.flightReplay.result?.route_id===' + json.dumps(gfs_id), 'More-dialog playback resolves the correct same-name route by route_id')
            await check('s.fixedRouteSelection.level==="100m AGL" && s.departureWindows.result.current_departure.status==="allowed"', 'Reload preserves the selected same-name route geometry and altitude')

            await begin()
            await run('s.selection.level="500m AGL"')
            await add(117.4, 30.9)
            await add(117.8, 30.6)
            missing_id = await save_and_evaluate('无预报仍可保存')
            report['missing_route_id'] = missing_id
            await check('s.departureWindows.result.current_departure.status==="unknown" && s.departureWindows.result.empty_reason==="data_insufficient" && document.body.innerText.includes("数据不足") && s.savedRoutes.some(r=>r.route_id===s.fixedRouteSelection.route_id)', 'Missing forecast is clearly shown while the fixed route stays saved')
            await screenshot('saved-missing-data.png')
            await call('Page.reload', {'ignoreCache': True})
            await wait("document.querySelector('#app')?.__vue_app__?._instance?.setupState?.savedRoutes?.some(r=>r.route_id===" + json.dumps(missing_id) + ')', 'Saved missing-data route survives full browser reload')
            await run("window.s=document.querySelector('#app').__vue_app__._instance.setupState")
            await wait('!s.loading', 'Map reload finishes')
            await run('s.runPlan(s.savedRoutes.find(r=>r.route_id===' + json.dumps(missing_id) + '))')
            await wait('s.fixedRouteSelection?.route_id===' + json.dumps(missing_id) + ' && s.departureWindows.result?.empty_reason==="data_insufficient"', 'Reloaded missing-data route re-enters the same fixed-path evaluator')
            await check('s.planner.algorithm==="wa_lpa_star" && !s.controlPanelRef.$.setupState.plannerTypes.some(p=>p.value==="wrf_v3")', 'WA-LPA* default and hidden V3 selection are unchanged')
            forbidden = [r for r in report['requests'] if '/api/route/plan' in r['url'] or '/api/point?' in r['url']]
            assert not forbidden, forbidden
            assert not report['downloads'], report['downloads']
            assert not report['errors'], report['errors']
            report['checks'].append('Entire custom-route workflow invokes no route search, point-query request or browser download; snapshot analysis is allowed after fixed-route selection')
            report['success'] = True
            print('Browser checks:', len(report['checks']))
        except Exception as exc:
            report['error'] = str(exc)
            await screenshot('browser-failure.png')
            raise
        finally:
            (WORK / 'browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            await call('Page.close')


if __name__ == '__main__':
    asyncio.run(main())
