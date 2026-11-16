"""Isolated browser regression for page caching, supersession and tiny detours."""
import asyncio
import base64
import json
import os
from pathlib import Path
import time
from urllib.request import build_opener, ProxyHandler, Request

import websockets

WORK=Path(os.getenv('REPLANNING_BROWSER_REPORT_DIR',str(Path(__file__).resolve().parents[3]/'data/replanning_cache_tests')))
HTTP=build_opener(ProxyHandler({}))


async def main():
    tab=json.loads(HTTP.open(Request('http://127.0.0.1:9228/json/new?about:blank',method='PUT')).read())
    report={'checks':[],'errors':[],'requests':[],'writes_normal_history':False}
    async with websockets.connect(tab['webSocketDebuggerUrl'],max_size=None) as socket:
        sequence=0
        async def call(method,params=None):
            nonlocal sequence
            sequence+=1
            await socket.send(json.dumps(dict(id=sequence,method=method,params=params or {})))
            while True:
                event=json.loads(await socket.recv())
                if event.get('method')=='Runtime.exceptionThrown': report['errors'].append(event['params'])
                if event.get('method')=='Network.requestWillBeSent':
                    req=event['params']['request']
                    if '/api/' in req['url'] and '/map-tiles/' not in req['url']:
                        report['requests'].append({k:req[k] for k in ('url','method','postData') if k in req})
                if event.get('id')==sequence:
                    if 'error' in event: raise RuntimeError(event['error'])
                    return event.get('result',{})
        async def js(expr):
            value=await call('Runtime.evaluate',dict(expression=expr,returnByValue=True,awaitPromise=True))
            if 'exceptionDetails' in value: raise RuntimeError(value['exceptionDetails'])
            return value['result'].get('value')
        async def run(code): return await js('(async()=>{'+code+'})()')
        async def wait(expr,label,seconds=30):
            end=time.monotonic()+seconds
            while not await js('Boolean('+expr+')'):
                assert time.monotonic()<end,(label,await js('({error:s.error,task:s.planningTask,message:s.planner.planningMessage,recommendation:s.recommendation})'))
                await asyncio.sleep(.075)
        async def check(expr,label):
            assert await run('return Boolean('+expr+')'),label
            report['checks'].append(label)
        def creates():
            return [r for r in report['requests'] if r['method']=='POST' and r['url'].endswith(('/route/plan/jobs','/route/recommendation/jobs'))]
        async def finished(job):
            end=time.monotonic()+3
            while True:
                state=await js(f'fetch("/api/route/recommendation/jobs/{job}").then(r=>r.json())')
                if state['compute_finished']:return state
                assert time.monotonic()<end,state
                await asyncio.sleep(.025)
        await call('Page.enable');await call('Runtime.enable');await call('Network.enable')
        await call('Page.addScriptToEvaluateOnNewDocument',dict(source='Date.now=()=>1762302600000;'))
        await call('Emulation.setDeviceMetricsOverride',dict(width=1920,height=1080,deviceScaleFactor=1,mobile=False))
        await call('Page.navigate',dict(url='http://127.0.0.1:5175/'))
        await wait('document.querySelector("#app")?.__vue_app__?._instance?.setupState','Vue ready')
        await run('window.s=document.querySelector("#app").__vue_app__._instance.setupState;window.cacheApi=await import(performance.getEntriesByType("resource").find(p=>p.name.includes("/src/api.js")).name)')
        await wait('!s.loading && s.options.cycles?.length','Initial weather')
        await run('window.historyBefore=await fetch("/api/routes").then(r=>r.json());s.selection.source="wrf"')
        await wait('!s.loading && s.options.source.startsWith("WRF")','WRF ready')
        await run('s.selection.level="100m AGL";s.planner.strategy="distance_priority";s.planner.startText="117.2,30.8";s.planner.endText="117.4,30.8";s.planner.departureMode="now"')
        await asyncio.sleep(.35)
        start=time.monotonic()
        await run('void s.runPlan()')
        await wait('!s.planner.planning && s.planner.points.length>=2','Cold planning')
        report['cold_ui_seconds']=time.monotonic()-start
        await check('cacheApi.getPagePlanningCacheStats().entries===1 && s.flightReplay.result.analysis.navigation_allowed','A verified result enters the page-memory cache')
        await run('window.originalPoints=JSON.stringify(s.planner.points)')
        count=len(creates());start=time.monotonic()
        await run('void s.runPlan()')
        await wait('!s.planner.planning && s.planningTask.cache_hit','Exact cache reuse')
        report['cache_ui_seconds']=time.monotonic()-start
        assert len(creates())==count,'Cache hit started a redundant search'
        await check('JSON.stringify(s.planner.points)===originalPoints && s.flightReplay.result.analysis.navigation_allowed','Cached geometry is revalidated without a new planning job')
        await check('document.body.innerText.includes("重新优化航线")','Cache result exposes explicit continued optimization')
        await run('void s.runPlan({forceReplan:true})')
        await wait('!s.planner.planning && s.planner.points.length>=2 && !s.planningTask.cache_hit','Explicit reoptimization')
        assert len(creates())==count+1
        assert json.loads(creates()[-1]['postData'])['seed_routes']
        report['checks'].append('Explicit optimization submits cached geometry as an independently validated seed')
        await run('s.planner.strategy="balanced"')
        await asyncio.sleep(.3)
        await run('void s.runPlan()')
        await wait('!s.planner.planning && s.planner.points.length>=2','Different strategy search')
        assert json.loads(creates()[-1]['postData'])['planning_strategy']=='balanced'
        assert json.loads(creates()[-1]['postData'])['seed_routes']
        await check('!s.planningTask.cache_hit && cacheApi.getPagePlanningCacheStats().entries===2','Different strategies have distinct entries and share only validation seeds')
        count=len(creates())
        await run('s.planner.strategy="distance_priority"')
        await asyncio.sleep(.3)
        await run('void s.runPlan()')
        await wait('!s.planner.planning && s.planningTask.cache_hit','Returning to previous strategy')
        assert len(creates())==count
        report['checks'].append('Returning to a previous strategy reuses its own cached route')
        await run('s.planner.departureAt="2026-11-05T09:00";s.planner.departureMode="scheduled"')
        await wait('cacheApi.getPagePlanningCacheStats().entries===3 && !s.planner.planning','Changed departure cache separation')
        await check('Date.parse(s.flightReplay.result.departure_at)===Date.parse("2026-11-05T01:00:00Z") && !s.planningTask.cache_hit','Changed departure rebuilds actual flight timing and creates a separate entry')

        # Replanning through the real Vue actions, not just the standalone gate.
        await run('s.clearRoute();s.planner.startText="117.2,30.8";s.planner.endText="117.9,30.8";s.planner.departureMode="now"')
        await asyncio.sleep(.35)
        ids=[]
        for strategy in ['wind_avoidance','balanced','distance_priority']:
            await run(f's.planner.strategy={json.dumps(strategy)}')
            await asyncio.sleep(.15)
            await run('void s.runPlan()')
            await wait('s.recommendationJobId','New owned job ID')
            job=await js('s.recommendationJobId')
            if job not in ids:ids.append(job)
        assert len(ids)>=2,ids
        for job in ids[:-1]:
            state=await finished(job)
            assert state['status']=='cancelled' and state['result'] is None,state
        report['checks'].append('Rapid strategy changes stop and discard all superseded page-owned computations')
        await run('s.stopPlanning()')
        await finished(ids[-1])

        await run('s.clearRoute();s.beginCustomDrawing();s.drawing.add([117.201,30.801]);s.drawing.add([117.203,30.802]);await s.drawing.complete({...s.selection})')
        await wait('s.departureWindows.result && s.flightReplay.result','Tiny drawing baseline')
        start=time.monotonic()
        await run('void s.runDetourRecommendation()')
        await wait('!s.recommending && s.recommendation?.detour_search_skipped','Tiny detour filtered')
        report['same_cell_detour_seconds']=time.monotonic()-start
        await check('!s.recommendation.recommendations.length && !s.recommendation.attempts.length && s.recommendation.message.includes("同一风场网格")','A proven same-cell detour is discarded without launching pointless searches')
        await check('JSON.stringify(await fetch("/api/routes").then(r=>r.json()))===JSON.stringify(historyBefore)','Planning and cache tests never save route records')

        # pagehide must clear cached completed results as well as active jobs.
        await run('s.clearRoute();s.planner.startText="117.2,30.8";s.planner.endText="117.9,30.8";s.planner.strategy="wind_avoidance"')
        await asyncio.sleep(.3)
        await run('void s.runPlan()')
        await wait('s.recommendationJobId && s.planner.planning','Active job before pagehide')
        close_id=await js('s.recommendationJobId')
        await check('cacheApi.getPagePlanningCacheStats().entries>0','Completed results remain available before pagehide')
        start=time.monotonic()
        await run('window.dispatchEvent(new PageTransitionEvent("pagehide"))')
        await check('cacheApi.getPagePlanningCacheStats().entries===0 && cacheApi.getPagePlanningCacheStats().bytes===0','pagehide immediately frees all page route cache entries')
        closed=await finished(close_id)
        report['pagehide_release_seconds']=time.monotonic()-start
        assert closed['status']=='cancelled' and closed['result'] is None and not closed['workflow'].get('recommendations') and not closed['workflow'].get('attempts'),closed
        report['checks'].append('pagehide releases active computation and server result payloads')
        assert any('forget=true' in r['url'] and r['method']=='DELETE' for r in report['requests'])
        await call('Page.navigate',dict(url='about:blank'))
        await call('Page.navigate',dict(url='http://127.0.0.1:5175/'))
        await wait('document.querySelector("#app")?.__vue_app__?._instance?.setupState','Fresh page')
        await check('(await import(performance.getEntriesByType("resource").find(p=>p.name.includes("/src/api.js")).name)).getPagePlanningCacheStats().entries===0','A newly loaded page starts with an empty cache')
        assert not report['errors'],report['errors']
        report['checks'].append('No browser runtime exceptions')
        screenshot=await call('Page.captureScreenshot',dict(format='png',captureBeyondViewport=False))
        (WORK/'cache-browser.png').write_bytes(base64.b64decode(screenshot['data']))
        (WORK/'cache-browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k!='requests'},ensure_ascii=False))
        await call('Page.close')


if __name__=='__main__':asyncio.run(main())
