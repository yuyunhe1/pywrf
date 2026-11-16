"""Production-browser wind/replay regression, using only unsaved test geometry.

Requires a dedicated Chrome CDP instance on 9232. Does not import dev modules
or depend on Vue setup-state exposure. All route-persistence writes are blocked.
"""
import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener

import websockets

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'data/public_wind_frame_tests'
HTTP = build_opener(ProxyHandler({}))

BOOTSTRAP = r"""(() => {
  let leaflet;
  Object.defineProperty(window, 'L', { configurable: true,
    get() { return leaflet }, set(value) {
      leaflet = value;
      value.Map?.addInitHook(function () { window.testMap = this });
    }
  });
  window.component = name => {
    const seen = new Set();
    function visit(v) {
      if (!v || typeof v !== 'object' || seen.has(v)) return;
      seen.add(v);
      if (v.component?.type?.__name === name) return v.component;
      const inside = visit(v.component?.subTree);
      if (inside) return inside;
      if (Array.isArray(v.children)) for (const child of v.children) {
        const found = visit(child); if (found) return found;
      }
    }
    return visit(document.querySelector('#app')?._vnode);
  };
  window.cp = () => component('ControlPanel')?.props;
  window.mp = () => component('WindMap')?.props;
  window.ap = () => component('AnalysisPanel')?.props;
  window.rp = () => component('FlightReplayControls')?.props.replay;
  window.byLabel = name => document.querySelector(`[aria-label="${name}"]`);
  window.clickButton = text => {
    const button = [...document.querySelectorAll('button')].find(b =>
      b.textContent.trim() === text && b.getClientRects().length);
    if (!button || button.disabled) throw Error(`Unavailable button: ${text}`);
    button.click();
  };
  window.particles = () => Object.values(testMap?._layers || {}).filter(l => l.flightBandKey);
})();
"""


class CDP:
    def __init__(self, socket, report):
        self.socket = socket
        self.report = report
        self.sequence = 0
        self.pending = {}
        self.weather = set()
        self.tasks = set()
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        async for raw in self.socket:
            event = json.loads(raw)
            if 'id' in event:
                future = self.pending.pop(event['id'], None)
                if future and not future.done():
                    if 'error' in event:
                        future.set_exception(RuntimeError(event['error']))
                    else:
                        future.set_result(event.get('result', {}))
                continue
            method, params = event.get('method'), event.get('params', {})
            if method == 'Runtime.exceptionThrown':
                self.report['errors'].append(params)
            elif method == 'Network.requestWillBeSent':
                request = params['request']
                url = request['url']
                if '/assets/' in url:
                    self.report['assets'].append(url)
                if any(p in url for p in ('/api/wind', '/api/heatmap', '/api/flight-replay/wind')):
                    self.report['weather_requests'].append(dict(url=url, id=params['requestId'], at=time.monotonic()))
                    self.weather.add(params['requestId'])
                    self.report['max_weather_inflight'] = max(self.report['max_weather_inflight'], len(self.weather))
            elif method in ('Network.loadingFinished', 'Network.loadingFailed'):
                if params['requestId'] in self.weather:
                    self.weather.remove(params['requestId'])
                    if method.endswith('Failed'):
                        self.report['weather_failures'].append(params)
            elif method == 'Fetch.requestPaused':
                task = asyncio.create_task(self.persistence_guard(params))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def persistence_guard(self, params):
        request = params['request']
        if request['method'] not in ('GET', 'HEAD', 'OPTIONS'):
            self.report['blocked_history_writes'].append(request)
            await self.call('Fetch.failRequest', dict(requestId=params['requestId'], errorReason='BlockedByClient'))
        else:
            await self.call('Fetch.continueRequest', dict(requestId=params['requestId']))

    async def call(self, method, params=None):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.socket.send(json.dumps(dict(id=request_id, method=method, params=params or {})))
        return await asyncio.wait_for(future, timeout=90)

    async def js(self, expression):
        result = await self.call('Runtime.evaluate', dict(expression=expression, awaitPromise=True, returnByValue=True))
        if result.get('exceptionDetails'):
            raise RuntimeError(result['exceptionDetails'])
        return result['result'].get('value')

    async def wait(self, expression, label, seconds=60):
        end = time.monotonic() + seconds
        while not await self.js(f'Boolean({expression})'):
            if time.monotonic() >= end:
                detail = await self.js('document.body.innerText.slice(-1800)')
                raise AssertionError((label, detail))
            await asyncio.sleep(.1)

    async def check(self, expression, label):
        assert await self.js(f'(async()=>Boolean({expression}))()'), label
        self.report['checks'].append(label)
        print(label, flush=True)

    async def choose(self, index, text):
        await self.js(f'document.querySelectorAll(".el-select__wrapper")[{index}].click()')
        option = f'''[...document.querySelectorAll('li.el-select-dropdown__item')].find(e=>
          e.getClientRects().length && e.textContent.includes({json.dumps(text)}))'''
        await self.wait(option, f'select {text}')
        await self.js(f'({option}).click()')

    async def click(self, selector, x=.5, y=.5):
        options = json.dumps(dict(selector=selector, x=x, y=y))
        box = await self.js('(()=>{const o=' + options + ''';const e=document.querySelector(o.selector);
          e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();
          return {x:r.left+r.width*o.x,y:r.top+r.height*o.y}})()''')
        for kind in ('mousePressed', 'mouseReleased'):
            await self.call('Input.dispatchMouseEvent', dict(type=kind, **box, button='left', clickCount=1))


async def verify(base, public_smoke=False):
    public = urlparse(base).hostname not in ('localhost', '127.0.0.1')
    name = 'browser-public' if public_smoke else 'browser-public-replay' if public else 'browser-production'
    report = dict(base_url=base, checks=[], errors=[], assets=[], weather_requests=[],
                  weather_failures=[], max_weather_inflight=0, blocked_history_writes=[],
                  writes_platform_history=False)
    tab = json.load(HTTP.open(Request('http://127.0.0.1:9232/json/new?about:blank', method='PUT')))
    async with websockets.connect(tab['webSocketDebuggerUrl'], max_size=None) as socket:
        c = CDP(socket, report)
        try:
            await c.call('Page.enable')
            await c.call('Runtime.enable')
            await c.call('Network.enable')
            await c.call('Network.setBlockedURLs', dict(urls=['*/api/map-tiles/*']))
            await c.call('Fetch.enable', dict(patterns=[dict(urlPattern='*/api/routes*', requestStage='Request')]))
            await c.call('Emulation.setDeviceMetricsOverride', dict(width=1920, height=1080, deviceScaleFactor=1, mobile=False))
            await c.call('Page.addScriptToEvaluateOnNewDocument', dict(source=BOOTSTRAP))
            await c.call('Page.navigate', dict(url=base))
            await c.wait('cp() && mp()?.metadata && !cp().loading', 'Initial production GFS draw', 90)
            report['release'] = await c.js('fetch("/release.json").then(r=>r.json())')
            await c.check('![...document.scripts].some(s=>s.src.includes("@vite")) && mp().wind.velocity.length===2',
                          'Production build renders the unified real GFS field')
            await c.check('!performance.getEntriesByType("resource").some(r=>r.name.includes("/assets/charts-"))',
                          'Chart code stays off the initial map loading path')
            await c.js('window.beforeHistory=fetch("/api/routes").then(r=>r.json())')
            await c.choose(0, 'WRF')
            await c.wait('cp().selection.source==="wrf" && !cp().loading && mp()?.metadata?.wind_time_model==="NATIVE_WRF_TEN_MINUTE_HOLD_V1"', 'Native WRF draw', 90)
            await c.js('void testMap.fitBounds([[29.5,116.6],[32.5,120.5]],{animate:false})')
            await c.wait('particles()[0]?._windy?.statistics.fieldReady', 'Native particle first draw')
            await c.check('particles().length===1', 'Only one shared particle engine is active')
            report['particle_resources'] = await c.js('particles()[0]._windy.statistics')
            await c.check('particles()[0]._windy.statistics.particles<=6000 && particles()[0]._windy.statistics.fieldVectors<=250000',
                          'Particle and projected-field limits hold')
            await c.check('mp().wind.metadata.valid_time===mp().metadata.valid_time', 'Snapshot time labels acknowledge the painted frame')

            await c.js('clickButton("风速热力图")')
            await c.wait('cp().layers.heatmap && document.querySelector(".wind-speed-canvas-layer")', 'Heatmap canvas')
            first = await c.js('cp().selection.validTime')
            second = await c.js('(()=>{const t=cp().options.valid_times;return t[t.findIndex(x=>x.label===cp().selection.validTime)+1].label})()')
            await c.js('window.heatCanvas=document.querySelector(".wind-speed-canvas-layer")')
            began = time.monotonic()
            await c.choose(2, second)
            await c.js('clickButton("加载风场")')
            await c.wait('!cp().loading && mp()?.metadata?.forecast_minutes===Math.round(cp().selection.forecastHour*60)', 'Other native snapshot')
            report['uncached_snapshot_seconds'] = round(time.monotonic()-began, 4)
            before = len(report['weather_requests'])
            began = time.monotonic()
            await c.choose(2, first)
            await c.js('clickButton("加载风场")')
            await c.wait('!cp().loading && mp()?.metadata?.forecast_minutes===Math.round(cp().selection.forecastHour*60)', 'Cached native snapshot')
            report['cached_snapshot_seconds'] = round(time.monotonic()-began, 4)
            report['cached_snapshot_weather_requests'] = len(report['weather_requests'])-before
            assert report['cached_snapshot_weather_requests'] == 0
            report['checks'].append('Cached snapshot changes send zero weather HTTP requests')
            await c.check('heatCanvas===document.querySelector(".wind-speed-canvas-layer")', 'Heatmap changes reuse the existing canvas')
            await c.choose(1, '250-350')
            await c.js('clickButton("加载风场")')
            await c.wait('!cp().loading && mp()?.metadata?.level==="250-350m AGL average"', 'Average layer draw', 90)
            await c.check('mp().heatmap.wind_speed.data.some(Number.isFinite)', '250–350m mean height field renders real data')
            await c.choose(1, '100m AGL')
            await c.js('clickButton("加载风场")')
            await c.wait('!cp().loading && mp()?.metadata?.level==="100m AGL"', 'Return to 100m')
            if not public_smoke:
                await verify_replay(c, report)
            await c.check('JSON.stringify(await beforeHistory)===JSON.stringify(await fetch("/api/routes").then(r=>r.json()))',
                          'Normal saved route history remains unchanged')
            assert not report['blocked_history_writes'], 'A route save was attempted'
            assert not report['errors'], report['errors']
            old = [row['url'] for row in report['weather_requests'] if not any(p in row['url'] for p in ('/api/wind/frame?', '/api/wind/frames?'))]
            assert not old, old
            assert report['max_weather_inflight'] <= 2
            report['checks'].append('Unified weather requests respect the two-request limit; no browser runtime exceptions')
            screenshot = await c.call('Page.captureScreenshot', dict(format='png'))
            (WORK / f'{name}.png').write_bytes(base64.b64decode(screenshot['data']))
            report['passed'] = True
        except Exception as error:
            report['passed'] = False
            report['failure'] = str(error)
            report['state'] = await c.js('({text:document.body.innerText.slice(-2200),selection:{...cp()?.selection},metadata:{...mp()?.metadata},replay:rp()&&{loading:rp().loading,mapLoading:rp().mapLoading,mapError:rp().mapError,error:rp().error,seconds:rp().seconds,mapTime:rp().mapTime}})')
            raise
        finally:
            (WORK / f'{name}.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            await c.call('Page.close')
            c.reader.cancel()


async def verify_replay(c, report):
    await c.js('clickButton("风向粒子流");clickButton("自定义绘制航线")')
    await c.wait('mp().customDrawing?.active', 'Custom drawing mode')
    await c.js('testMap.fire("click",{latlng:L.latLng(30.91165,117.17468)});void testMap.fire("click",{latlng:L.latLng(30.39657,118.13049)})')
    await c.check('mp().customDrawing.points.length===2', 'Real map clicks create the unsaved long route in order')
    await c.js('clickButton("完成绘制并评估")')
    await c.wait('rp()?.result && !rp().mapLoading && rp().mapTime && particles()[0]?._windy?.statistics.fieldReady', 'Long native WRF replay ready', 150)
    await c.check('rp().result.flight_duration_seconds>7200 && rp().result.wind_time_model==="NATIVE_WRF_TEN_MINUTE_HOLD_V1"', 'Long replay uses the same real native dataset')
    report['profile'] = await c.js('({dataset_id:rp().result.dataset_id,departure_at:rp().result.departure_at,duration:rp().result.flight_duration_seconds,route_id:rp().result.route_id})')
    await c.check('ap().departureWindows.result?.departure_windows?.length && Date.parse(rp().result.departure_at)===Date.parse(ap().departureWindows.result.departure_windows[0].start)',
                  'Drawn route defaults to its earliest confirmed flying window')
    await c.js('window.firstReplayWind=mp().wind;window.firstReplayTime=rp().mapTime')
    await seek(c, 900)
    await c.check('mp().wind===firstReplayWind && rp().mapTime===firstReplayTime', 'Particle background holds during the first thirty flight minutes')
    await asyncio.sleep(2)
    before = len(report['weather_requests'])
    began = time.monotonic()
    await seek(c, 1800)
    await c.wait('!rp().mapLoading && rp().mapTime!==firstReplayTime', 'Prefetched frame first draw')
    report['prefetched_replay_draw_seconds'] = round(time.monotonic()-began, 4)
    at = await c.js('rp().mapTime')
    assert not any(parse_qs(urlparse(row['url']).query).get('at') == [at] for row in report['weather_requests'][before:])
    report['checks'].append('Prefetched replay frame draws without downloading that frame again')
    await c.check('mp().wind.metadata.valid_time===rp().mapTime && mp().metadata.valid_time===rp().mapTime',
                  'Painted background, replay label and map metadata use the same native frame')
    await c.wait('document.querySelector(".wind-chart canvas")', 'Lazy route chart')
    await c.click('.wind-chart canvas', .55, .45)
    await c.wait('mp().selectedSampleIndex!=null && Object.values(testMap._layers).some(l=>l.options?.pane==="routeSelectionPane" && l.options.color==="#ff3b30")', 'Curve red map locator')
    await c.check('mp().selectedSampleIndex!=null', 'Clicking the real chart highlights the corresponding route point in red')
    await c.js('document.querySelectorAll(".legend-block")[4].click()')
    await c.check('particles()[0].options.visibleBands.join()==="4" && [1.5,3.3,5.4,7.9,8].every((s,i)=>particles()[0].options.colorIndex(s)===i)',
                  'Legend filtering and all five original wind-grade boundaries remain correct')
    await c.js('document.querySelectorAll(".legend-block")[4].click()')
    await c.call('Network.emulateNetworkConditions', dict(offline=False, latency=2000, downloadThroughput=350000, uploadThroughput=350000))
    await seek(c, 5340)
    await c.wait('!rp().mapLoading', 'Seek to cached sixty-minute background')
    await c.js('window.slowStartSeconds=rp().seconds;window.slowStartTime=rp().mapTime;const r=byLabel("回放倍速");r.value="600";r.dispatchEvent(new Event("change",{bubbles:true}));byLabel("播放航程").click()')
    await c.wait('rp().mapLoading && rp().seconds>=slowStartSeconds+300', 'Flight progresses while slow future background is pending', 12)
    await c.check('rp().playing && rp().mapTime===slowStartTime && !byLabel("暂停回放").disabled',
                  'Slow weather keeps the old labelled background and leaves playback/pause responsive')
    await c.js('byLabel("暂停回放").click()')
    await c.check('!rp().playing', 'Pause responds while the weather download is pending')
    await c.wait('!rp().mapLoading && rp().mapTime!==slowStartTime', 'Coalesced slow frame catches up', 30)
    await c.call('Network.emulateNetworkConditions', dict(offline=False, latency=0, downloadThroughput=-1, uploadThroughput=-1))
    batches = [row['url'] for row in report['weather_requests'] if '/api/wind/frames?' in row['url']]
    assert batches and all(len(parse_qs(urlparse(url).query).get('at', [])) <= 2 and 'at%5B' not in url and 'at[]' not in url for url in batches)
    report['checks'].append('Replay batches two repeated at parameters and completes slow automatic requests')
    await c.check('particles().length===1 && particles()[0]._windy.statistics.particles<=6000 && particles()[0]._windy.statistics.fieldVectors<=250000',
                  'Long replay keeps a single bounded renderer after multiple frame changes')
    await c.call('Emulation.setDeviceMetricsOverride', dict(width=1366, height=768, deviceScaleFactor=1, mobile=False))
    await asyncio.sleep(.4)


async def seek(c, seconds):
    await c.js(f'''(()=>{{const e=document.querySelector('input[aria-label="飞行回放进度"]');
      e.value={seconds};e.dispatchEvent(new Event('input',{{bubbles:true}}));}})()''')
    await c.wait(f'Math.abs(rp().seconds-{seconds})<.01', f'seek {seconds}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:5174/')
    parser.add_argument('--public-smoke', action='store_true')
    args = parser.parse_args()
    asyncio.run(verify(args.base, args.public_smoke))
