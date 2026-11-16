"""Browser acceptance on isolated workspace_smoke_server / Vite 5175 / CDP 9227.

Fixtures replace only ingestion. Save/export, all three search algorithms,
continuous decisions, windows and flight profiles use the real implementation.
"""
import asyncio
import base64
import json
from pathlib import Path
import time
from urllib.parse import quote
from urllib.request import build_opener, ProxyHandler, Request
import websockets

WORK = Path(__file__).resolve().parents[3] / 'data/ui_redesign_tests'
OPENER = build_opener(ProxyHandler({}))


def api(path, body=None, method=None):
    response = OPENER.open(Request('http://127.0.0.1:8002/api/' + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'}, method=method), timeout=30)
    raw = response.read()
    return json.loads(raw) if raw else None


async def main():
    WORK.mkdir(parents=True, exist_ok=True)
    assert api('times?source=gfs')['source'].endswith('UI acceptance fixture')
    for row in api('routes'):
        api('routes/' + row['route_id'], method='DELETE')
    seeds = {}
    for key, name, source, level in [('safe', '同名航线', 'gfs', '100m AGL'),
                                    ('future', '同名航线', 'wrf', '200m AGL'),
                                    ('blocked', '无可飞窗口', 'gfs', '300m AGL'),
                                    ('missing', '预报不足', 'wrf', '500m AGL')]:
        points = [[117.2, 30.8], [117.6, 30.7], [117.95, 30.8]]
        seeds[key] = api('routes', {'name': name, 'source': source, 'level': level, 'points': points,
                                     'start': points[0], 'end': points[-1]})
    tab = json.loads(OPENER.open(Request('http://127.0.0.1:9227/json/new?about:blank', method='PUT')).read())
    report = {'checks': [], 'errors': [], 'requests': [], 'downloads': [], 'seeds': seeds,
              'scope': 'Isolated storage and deterministic hourly weather; real planner/window/profile; tiles blocked'}
    async with websockets.connect(tab['webSocketDebuggerUrl'], max_size=None) as socket:
        seq = 0

        async def call(method, params=None):
            nonlocal seq
            seq += 1
            await socket.send(json.dumps({'id': seq, 'method': method, 'params': params or {}}))
            while True:
                event = json.loads(await socket.recv())
                if event.get('method') == 'Runtime.exceptionThrown': report['errors'].append(event['params'])
                if event.get('method') == 'Network.requestWillBeSent':
                    request = event['params']['request']
                    if '/api/' in request['url'] and '/map-tiles/' not in request['url']:
                        report['requests'].append({k: request[k] for k in ('url', 'method', 'postData') if k in request})
                if event.get('method') == 'Page.downloadWillBegin': report['downloads'].append(event['params'])
                if event.get('id') == seq:
                    if 'error' in event: raise RuntimeError(event['error'])
                    return event.get('result', {})

        async def js(code):
            result = await call('Runtime.evaluate', {'expression': code, 'returnByValue': True, 'awaitPromise': True})
            if 'exceptionDetails' in result: raise RuntimeError(result['exceptionDetails'])
            return result['result'].get('value')

        async def run(code):
            return await js('(async()=>{' + code + ';})()')

        async def check(code, label):
            assert await js('Boolean(' + code + ')'), label
            report['checks'].append(label)

        async def wait(code, label, timeout=30):
            end = time.monotonic() + timeout
            while not await js('Boolean(' + code + ')'):
                assert time.monotonic() < end, label + ': ' + str(await js('document.body.innerText.slice(-2000)'))
                await asyncio.sleep(.1)

        async def viewport(width, height):
            await call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False})
            await asyncio.sleep(.2)

        async def xy(x, y, kind='click'):
            if kind == 'move':
                await call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
            else:
                for event in ('mousePressed', 'mouseReleased'):
                    await call('Input.dispatchMouseEvent', {'type': event, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
            await asyncio.sleep(.1)

        async def click(expression):
            await run(f'({expression}).scrollIntoView({{block:"center"}})')
            point = await js(f'(()=>{{const r=({expression}).getBoundingClientRect();return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()')
            await xy(**point)

        def button(label, root='document'):
            return f'[...{root}.querySelectorAll("button")].find(b=>b.textContent.trim()==={json.dumps(label)})'

        async def screenshot(name):
            value = await call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
            (WORK / name).write_bytes(base64.b64decode(value['data']))

        async def ready():
            await wait('document.querySelector("#app")?.__vue_app__?._instance?.setupState?.savedRoutes?.length', 'Application loads history')
            await run('window.s=document.querySelector("#app").__vue_app__._instance.setupState')
            await wait('!s.loading', 'Initial wind loaded')

        async def select(key):
            route_id = seeds[key]['route_id']
            await run('void s.runPlan(s.savedRoutes.find(r=>r.route_id===' + json.dumps(route_id) + '))')
            await wait('s.flightReplay.result?.route_id===' + json.dumps(route_id) + ' && s.departureWindows.result?.route_id===' + json.dumps(route_id) + ' && !s.flightReplay.loading && !s.departureWindows.loading && !s.loading', 'Selected route completes its assessment')
            await asyncio.sleep(.2)

        async def plan(algorithm):
            await run('s.clearRoute();s.selection.source="gfs"')
            await wait('!s.loading && s.options.source.startsWith("GFS")', 'GFS selection loaded')
            await run('s.selection.level="100m AGL";s.planner.algorithm=' + json.dumps(algorithm) + ';s.planner.startText="117.2,30.8";s.planner.endText="117.4,30.8";s.planner.name="规划验收";s.planner.departureMode="now"')
            await asyncio.sleep(.15)
            await click(button('生成航线', 'document.querySelector(".planning-actions")'))
            await wait('!s.planner.planning && s.planner.points.length>1 && s.flightReplay.result?.analysis.navigation_allowed', 'Algorithm completes feasible plan: ' + algorithm, 60)
            await check('s.planningStatus.status==="completed" && s.planningStatus.label==="规划完成" && Number.isFinite(s.planningStatus.elapsed)', 'Verified route and real completion status: ' + algorithm)

        try:
            await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable')
            await call('Page.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': str(WORK / 'downloads')})
            await call('Network.setBlockedURLs', {'urls': ['*/api/map-tiles/*']})
            await viewport(1920, 1080)
            await call('Page.navigate', {'url': 'http://127.0.0.1:5175/'})
            await call('Page.bringToFront')
            await ready()
            await run('s.workspace.reset()')
            await check('s.panels.leftWidth===320 && s.panels.rightWidth===360 && s.panels.leftOpen && s.panels.rightOpen', 'Desktop starts with 320/360px expanded panels')
            await check('document.querySelector(".region-location") && !document.querySelector(".region-location").closest("details") && !document.querySelector(".advanced-settings").open', 'Region stays expanded and advanced settings start collapsed')
            await check('document.querySelector(".analysis-empty") && !document.querySelector(".core-metrics")', 'Empty decision panel gives an action instead of empty statistics')
            await check('getComputedStyle(document.querySelector(".control-panel label")).fontSize==="14px" && document.querySelector(".workspace-header").getBoundingClientRect().height===48', 'Readable body type and compact 48px brand bar')
            await screenshot('desktop-empty.png')
            before = len(report['requests'])
            await run('window.oldWindLayers=[...s.mapRef.$.setupState.velocityLayers];window.oldWind=s.wind')
            start = await js('(()=>{const r=document.querySelector(".panel-resizer.left").getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+100};})()')
            await call('Input.dispatchMouseEvent', {'type': 'mousePressed', **start, 'button': 'left', 'clickCount': 1})
            for step in range(1, 6):
                await call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': start['x'] + step * 30, 'y': start['y'], 'buttons': 1})
            await call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': start['x'] + 150, 'y': start['y'], 'button': 'left', 'clickCount': 1})
            await wait('!s.resizing && s.panels.leftWidth>450', 'Resize drag settles')
            await check('s.wind===oldWind && s.mapRef.$.setupState.velocityLayers.every((layer,i)=>layer===oldWindLayers[i]) && !s.planner.points.length', 'Dragging panels preserves weather layers and does not create route points')
            assert not any('/api/wind' in r['url'] or '/api/route/plan' in r['url'] for r in report['requests'][before:])
            report['checks'].append('Panel resizing sends no weather reload or route search')
            await click('document.querySelector("[aria-label=收起通航决策]")')
            await run('window.storedWidth=s.panels.leftWidth')
            await call('Page.reload')
            await ready()
            await check('s.panels.leftWidth>450 && !s.panels.rightOpen', 'Panel width and collapsed state survive reload')
            await click('document.querySelector("[aria-label=恢复默认布局]")')
            await viewport(1366, 768)
            await check('1366-s.insets.left-s.insets.right>=400 && s.panels.leftOpen && s.panels.rightOpen', '1366px viewport retains at least 400px of map')
            await run('document.querySelector(".control-panel .panel-content").scrollTop=9999')
            await check('(()=>{const f=document.querySelector(".planning-actions").getBoundingClientRect();return f.bottom<=innerHeight-12 && f.top>72;})()', 'Primary actions stay visible while the form scrolls')
            await screenshot('desktop-1366.png')
            await viewport(1200, 800)
            await check('s.compact && s.panels.leftOpen && !s.panels.rightOpen && !document.querySelector(".panel-resizer")', '1200px breakpoint switches to a single drawer')
            await click(button('决策', 'document.querySelector(".workspace-header")'))
            await check('!s.panels.leftOpen && s.panels.rightOpen', 'Explicit drawer switching opens only one panel')
            await click(button('决策', 'document.querySelector(".workspace-header")'))
            await xy(850, 300, 'move')
            await check('!s.panels.leftOpen && !s.panels.rightOpen', 'Hovering the map does not reopen drawers')
            await viewport(390, 844)
            await click(button('航线', 'document.querySelector(".workspace-header")'))
            await check('document.documentElement.scrollWidth===390 && document.querySelector(".control-panel").getBoundingClientRect().right<=390 && document.querySelector(".planning-actions").getBoundingClientRect().bottom<=832', '390px layout contains the drawer and all primary actions')
            await screenshot('mobile-drawer.png')
            await select('safe')
            await check('s.panels.rightOpen && !s.panels.leftOpen', 'First route selection opens the decision drawer')
            await click(button('决策', 'document.querySelector(".workspace-header")'))
            await run('await s.departureEvaluator.refresh()')
            await check('!s.panels.rightOpen && !s.panels.leftOpen', 'Background evaluation respects a manually closed drawer')
            await viewport(1920, 1080)
            await run('s.workspace.reset()')
            await select('future')
            await check('s.departureWindows.result.current_departure.status==="blocked" && s.departureWindows.result.departure_windows.length>0 && document.querySelector(".primary-decision").innerText.includes("禁止通航")', 'Cross-hour danger and a future safe window are shown separately')
            await check('[...document.querySelector(".analysis-body .panel-content").children].slice(0,4).map(e=>e.className).join("|").includes("primary-decision|departure-settings|departure-windows|core-metrics-section")', 'Decision, timing, windows and core metrics appear in priority order')
            await check('document.querySelectorAll(".core-metric").length===4 && !document.querySelector(".analysis-details").open && getComputedStyle(document.querySelector(".flight-summary")).color==="rgb(255, 255, 255)"', 'Four core metrics, collapsed details and white flight summary')
            await check('document.querySelector(".window-duration").innerText.includes("起飞窗口长度") && !document.querySelector(".departure-window-list button") && document.querySelector(".departure-settings input").step==="60"', 'Windows are static and distinguish departure interval length from flight duration')
            await screenshot('desktop-decision.png')
            await select('blocked')
            await check('document.querySelector(".departure-windows").innerText.includes("已覆盖的未来航程均触发") && !document.querySelector(".departure-window-list li")', 'No-window result has its own explicit state')
            await select('missing')
            await check('document.querySelector(".primary-decision").innerText.includes("数据不足") && document.querySelector(".core-metric strong").textContent.includes("—")', 'Missing data is not presented as a zero wind speed')
            await select('safe')
            await run('document.querySelector(".analysis-panel .panel-content").scrollTop=0')
            await screenshot('desktop-safe.png')

            # Additional workflow acceptance is appended below this point.
            for algorithm in ('astar', 'lpa_star', 'wa_lpa_star'):
                await plan(algorithm)

            await check('document.querySelector(".export-trigger button").disabled', 'Unsaved plan cannot export a previous route')
            await run('window.unsavedGeometry=JSON.stringify(s.planner.points)')
            await call('Network.setBlockedURLs', {'urls': ['*/api/map-tiles/*', '*/api/routes']})
            await click(button('保存航线', 'document.querySelector(".planning-actions")'))
            await wait('s.saveError && !s.savingRoute', 'Save failure is reported locally')
            await check('JSON.stringify(s.planner.points)===unsavedGeometry && !s.savedCurrent', 'Failed save retains the route and permits retry')
            await call('Network.setBlockedURLs', {'urls': ['*/api/map-tiles/*']})
            await run('const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.open=function(method,url,...rest){this.testMethod=method.toUpperCase();this.testUrl=url;return open.call(this,method,url,...rest)};XMLHttpRequest.prototype.send=function(body){if(window.holdSave && this.testMethod==="POST" && this.testUrl==="/api/routes"){window.holdSave=false;window.heldPayload=JSON.parse(body);window.releaseSave=()=>send.call(this,body);return}return send.call(this,body)};window.holdSave=true')
            await click(button('保存航线', 'document.querySelector(".planning-actions")'))
            await wait('s.savingRoute && window.releaseSave', 'Save is held in flight')
            await run('void s.persistRoute();s.planner.points=s.planner.points.map((p,i)=>i===1?[p[0],p[1]+.001,...p.slice(2)]:p);releaseSave()')
            await wait('!s.savingRoute && s.savedReceipt', 'Held save returns')
            await check('JSON.stringify(s.savedReceipt.record.points)===unsavedGeometry && !s.savedCurrent && document.querySelector(".export-trigger button").disabled', 'Late save persists the submitted geometry and cannot mark a replacement as saved')
            assert len(api('routes')) == len(seeds) + 1
            report['checks'].append('Repeated submission while saving creates only one record')
            await plan('wa_lpa_star')
            await run('s.planner.name="保存导出验收"')
            await click(button('保存航线', 'document.querySelector(".planning-actions")'))
            await wait('!s.savingRoute && s.savedCurrent && !s.saveError', 'Save retry completes')
            saved_id = await js('s.savedCurrent.route_id')
            saved_count = len(api('routes'))
            await run('await s.persistRoute();await s.persistRoute()')
            assert len(api('routes')) == saved_count
            assert not report['downloads']
            await check('document.querySelector(".export-trigger button").disabled===false && [...document.querySelector(".planning-actions").querySelectorAll("button")].some(b=>b.textContent.trim()==="已保存" && b.disabled)', 'Saved current version is marked saved and saving alone triggers no download')
            expected_file = next(row for row in api('exported-routes') if row['route_id'] == saved_id)
            await click('document.querySelector(".export-trigger button")')
            await click(button('导出 JSON', 'document.querySelector(".route-export-popover")'))
            await click(button('导出航点文件', 'document.querySelector(".route-export-popover")'))
            await asyncio.sleep(.3)
            await js('true')
            assert len(report['downloads']) == 2, report['downloads']
            from urllib.parse import unquote
            assert unquote(report['downloads'][0]['url']).endswith(expected_file['file_name'])
            assert unquote(report['downloads'][1]['url']).endswith(expected_file['waypoint_file_name'])
            report['checks'].append('JSON and waypoint export download exactly the selected saved version')
            await xy(1100, 300)
            await run('s.planner.name="修改后的航线"')
            await check('!s.savedCurrent && document.querySelector(".export-trigger button").disabled', 'Changing the current name invalidates its saved/export binding')
            assert api('routes/' + saved_id)['name'] == '保存导出验收'
            report['checks'].append('Editing the form does not overwrite the saved record')
            await run('s.planner.name="保存导出验收"')
            await check('s.savedCurrent?.route_id===' + json.dumps(saved_id), 'Returning to unchanged saved geometry restores its identity')
            await run('await s.removeRoute(' + json.dumps(saved_id) + ')')
            await check('!s.savedCurrent && document.querySelector(".export-trigger button").disabled', 'Deleting the saved record invalidates a cached save receipt')

            await run('const e=document.querySelector("[aria-label=搜索历史航线]");e.value="同名";e.dispatchEvent(new Event("input",{bubbles:true}))')
            await check('document.querySelectorAll(".saved-route").length===2 && document.querySelector(".saved-route-meta").textContent.includes("AGL") && document.querySelector(".saved-route-meta").textContent.includes("km")', 'Sidebar search shows source, height and computed distance for same-name routes')
            future_selector = 'document.querySelector(' + json.dumps('[data-route-id="' + seeds['future']['route_id'] + '"] .saved-route-button') + ')'
            await click(future_selector)
            await wait('s.fixedRouteSelection?.route_id===' + json.dumps(seeds['future']['route_id']) + ' && !s.departureWindows.loading', 'Same-name record loads by ID')
            await check('s.selection.source==="wrf" && s.selection.level==="200m AGL"', 'Same-name history chooses the correct source and altitude')
            route_article = 'document.querySelector(' + json.dumps('[data-route-id="' + seeds['future']['route_id'] + '"]') + ')'
            await click(button('导出', route_article))
            await wait('[...document.querySelectorAll(".route-export-popover")].some(e=>getComputedStyle(e).display!=="none" && e.getAttribute("aria-hidden")!=="true")', 'Sidebar export menu opens')
            menu = '[...document.querySelectorAll(".route-export-popover")].find(e=>getComputedStyle(e).display!=="none" && e.getAttribute("aria-hidden")!=="true")'
            await click(button('导出 JSON', menu))
            await asyncio.sleep(.25)
            await js('true')
            assert unquote(report['downloads'][-1]['url']).endswith(seeds['future']['exported_json']['file_name'])
            report['checks'].append('Same-name sidebar export downloads the file belonging to the selected ID')
            await xy(1100, 300)
            await click(button('更多...'))
            await wait('document.querySelector(".el-dialog__body")', 'History dialog opens')
            await run('const e=document.querySelector("[aria-label=搜索历史航线列表]");e.value="预报不足";e.dispatchEvent(new Event("input",{bubbles:true}))')
            await wait('document.querySelectorAll(".el-table__body-wrapper tbody tr").length===1', 'More-dialog search filters records')
            await check('document.querySelector(".el-table__body-wrapper tbody").innerText.includes("WRF") && document.querySelector(".el-table__body-wrapper tbody").innerText.includes("500m AGL")', 'More-dialog records include source and altitude metadata')
            await click('document.querySelector(".el-table__body-wrapper tbody .el-button--success")')
            await wait('s.fixedRouteSelection?.route_id===' + json.dumps(seeds['missing']['route_id']) + ' && !document.querySelector(".el-dialog__body")', 'Filtered history playback uses the exact record ID')

            await select('safe')
            await wait('!s.flightReplay.mapLoading && s.flightReplay.mapWind', 'Replay background is ready')
            await run('s.replay.play()')
            await click(button('更多...'))
            await wait('document.querySelector(".el-dialog__body")', 'Drawing entry dialog opens')
            await click(button('自定义绘制航线', 'document.querySelector(".el-dialog__body")'))
            await wait('s.customDrawing.active && !document.querySelector(".el-dialog__body")', 'Custom drawing starts')
            await run('window.m=s.mapRef.$.setupState.map;m.stop().setView([30.75,117.6],10,{animate:false})')
            for lon, lat in [(117.25, 30.9), (117.6, 30.65), (117.95, 30.9)]:
                point = await js(f'(()=>{{const p=m.latLngToContainerPoint([{lat},{lon}]),r=m.getContainer().getBoundingClientRect();return {{x:r.x+p.x,y:r.y+p.y}};}})()')
                await xy(**point)
            await check('s.customDrawing.points.length===3 && !s.flightReplay.playing && !s.departureWindows.active && !document.querySelector(".leaflet-draw")', 'Drawing starts cleanly and ordered clicks produce three waypoints')
            await run('window.draftGeometry=JSON.stringify(s.customDrawing.points);s.workspace.close("left");s.workspace.open("left");s.selection.source="wrf"')
            await wait('!s.loading && s.options.source.startsWith("WRF")', 'WRF preview loads')
            await run('s.selection.level="200m AGL";s.flightSettings.averageGroundSpeedMps=25;await s.loadWindField()')
            await check('JSON.stringify(s.customDrawing.points)===draftGeometry && s.customDrawing.active', 'Panel collapse and weather selection preserve the drawing draft')
            await check('(()=>{const legend=document.querySelector(".wind-heat-legend").getBoundingClientRect(),dock=document.querySelector(".map-dock").getBoundingClientRect();return legend.bottom<=dock.top && dock.bottom<=innerHeight-12;})()', 'Measured drawing toolbar height keeps the legend clear')
            downloads_before = len(report['downloads'])
            await click(button('保存固定航线', 'document.querySelector(".custom-route-controls")'))
            await wait('!s.customDrawing.active && s.savedCurrent && s.departureWindows.result?.source==="wrf" && !s.departureWindows.loading && !s.flightReplay.loading', 'Drawn route is saved and evaluated')
            await check('JSON.stringify(s.savedCurrent.points)===draftGeometry && s.savedCurrent.level==="200m AGL" && s.flightReplay.result.average_ground_speed_mps===25', 'Drawing save retains exact geometry and current source/height/speed')
            assert len(report['downloads']) == downloads_before
            report['checks'].append('Custom drawing save remains separate from browser downloads')
            await wait('!s.flightReplay.mapLoading', 'Flight wind loads')
            await run('window.windBeforeSeek=s.flightReplay.mapWind;window.timeBeforeSeek=s.flightReplay.mapTime;await s.replay.seek(60)')
            await check('s.flightReplay.mapWind===windBeforeSeek && s.flightReplay.mapTime===timeBeforeSeek && document.querySelector(".flight-aircraft-icon")', 'Replay still moves the aircraft while reusing the particle background')
            await run('await s.replay.seek(1800)')
            await check('s.flightReplay.mapTime!==timeBeforeSeek', 'Particle background updates at the 30-minute flight boundary')
            await run('window.compactReplayHeight=s.dockHeight')
            await click('document.querySelector(".replay-details summary")')
            await wait('s.dockHeight>compactReplayHeight', 'Expanded replay dock is measured')
            await check('(()=>{const legend=document.querySelector(".wind-heat-legend").getBoundingClientRect(),replay=document.querySelector(".flight-replay-controls").getBoundingClientRect();return legend.right<replay.x && Math.abs(legend.y-replay.y)<=1;})()', 'Legend stays beside playback when replay details expand')
            await click('document.querySelector(".replay-details summary")')
            await run('const c=await import("/src/utils/charts.js");window.chart=c.getInstanceByDom(document.querySelector(".wind-chart"));document.querySelector(".wind-chart").scrollIntoView({block:"center"})')
            point = await js('(()=>{const r=document.querySelector(".wind-chart").getBoundingClientRect(),p=chart.convertToPixel({seriesIndex:0},[s.flightReplay.result.analysis.total_distance_km/2,8]);return {x:r.x+p[0],y:r.y+p[1]};})()')
            await xy(**point)
            await check('s.selectedRouteSampleIndex!==null && document.querySelector(".leaflet-routeSelection-pane path")', 'Curve clicks still place the red map selection')
            await run('window.selectionBeforeCollapse=s.selectedRouteSampleIndex;s.workspace.close("right");s.workspace.open("right")')
            await check('s.selectedRouteSampleIndex===selectionBeforeCollapse && document.querySelector(".leaflet-routeSelection-pane path")', 'Collapsing a panel preserves curve/map selection')
            await run('window.chartWidthBefore=chart.getWidth();document.querySelector(".panel-resizer.right").focus()')
            await call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'ArrowLeft', 'code': 'ArrowLeft', 'windowsVirtualKeyCode': 37})
            await call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'ArrowLeft', 'code': 'ArrowLeft', 'windowsVirtualKeyCode': 37})
            await wait('chart.getWidth()>chartWidthBefore', 'Chart responds to right-panel resize')
            await check('s.panels.rightWidth===376 && s.selectedRouteSampleIndex===selectionBeforeCollapse', 'Right panel supports keyboard resizing without resetting chart selection')
            await screenshot('desktop-workflow.png')
            await run('s.beginCustomDrawing();s.drawing.add([117.3,30.8]);s.drawing.add([117.7,30.7])')
            await viewport(390, 844)
            await run('s.workspace.showMap();m.stop().setView([30.75,117.6],8,{animate:false})')
            await check('s.customDrawing.points.length===2 && document.querySelector(".custom-route-controls").getBoundingClientRect().right<=378 && document.querySelector(".custom-route-controls").getBoundingClientRect().bottom<=832', 'Mobile drawing retains the draft and keeps all toolbar actions on screen')
            await screenshot('mobile-drawing.png')
            count_before_cancel = len(api('routes'))
            await click(button('取消', 'document.querySelector(".custom-route-controls")'))
            await check('!s.customDrawing.active && !s.customDrawing.points.length', 'Drawing cancellation clears only its draft')
            assert len(api('routes')) == count_before_cancel
            report['checks'].append('Cancelling an unsaved drawing writes no route record')
            assert not report['errors'], report['errors']
            report['success'] = True
            print('Browser checks:', len(report['checks']))
        except Exception as reason:
            report['error'] = str(reason)
            try:
                report['last_state'] = await js('({panels:s.panels,planning:s.planner.planning,message:s.planner.planningMessage,points:s.planner.points.length,error:s.error})')
                await screenshot('failure.png')
            except Exception: pass
            raise
        finally:
            (WORK / 'browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            await call('Page.close')


if __name__ == '__main__':
    asyncio.run(main())
