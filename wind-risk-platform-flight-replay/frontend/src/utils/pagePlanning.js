import { isDisplayableRoute } from './routeQuality.js'
const clone = value => JSON.parse(JSON.stringify(value))
const canonical = value => JSON.stringify(value, (_key,item) => item && !Array.isArray(item) && typeof item === 'object'
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key,item[key]])) : item)
const abortError = () => Object.assign(new Error('规划已取消'),{name:'AbortError'})

export function createPageRouteCache({maxEntries=24,maxBytes=4*1024*1024,target=globalThis.window}={}) {
  const entries=new Map()
  let bytes=0,epoch=0
  const versionKey=(payload,version) => version?.forecast_revision && version?.planning_runtime_id
    ? canonical([payload.source,payload.level,payload.cycle,payload.valid_time,version.forecast_revision,version.planning_runtime_id,version.wind_time_model]) : null
  const endpoints=payload => canonical([payload.start,payload.end])
  const key=(payload,version) => {
    const weather=versionKey(payload,version)
    return weather ? canonical([weather,payload.start,payload.end,payload.planner_type,payload.aircraft_model,
      payload.planning_strategy,payload.thresholds,payload.wind_shear,payload.average_ground_speed_mps,payload.departure_at || 'now',
      Boolean(payload.prefer_simple_route),payload.mode,payload.required_waypoints,payload.departure_start_at,
      payload.departure_end_at,payload.selection_model]) : null
  }
  const clear=() => { entries.clear();bytes=0;epoch++ }
  target?.addEventListener('pagehide',clear)
  return {
    get epoch(){return epoch},
    get(payload,version) {
      const id=key(payload,version),entry=entries.get(id)
      if (!entry) return
      entries.delete(id);entries.set(id,entry)
      return clone(entry.plan)
    },
    put(payload,version,result,generation=epoch) {
      const id=key(payload,version)
      if (!id || generation!==epoch || result?.analysis?.navigation_allowed!==true || result.reference_route
        || payload.prefer_simple_route && !isDisplayableRoute(result)
        || result.source && result.source!==payload.source || result.level && result.level!==payload.level
        || !result.points?.length || endpoints({start:result.points[0].slice(0,2),end:result.points.at(-1).slice(0,2)})!==endpoints(payload)) return
      // Profiles, windows and search diagnostics are rebuilt on reuse; keep
      // exact spatial geometry, never a thinned display line or a search tree.
      const omit=new Set(['analysis','flight_profile','segments','departure_windows','planning_diagnostics'])
      const plan=clone(Object.fromEntries(Object.entries(result).filter(([name]) => !omit.has(name))))
      const size=canonical(plan).length*2
      if (size>maxBytes) return
      if (entries.has(id)) { bytes-=entries.get(id).size;entries.delete(id) }
      entries.set(id,{plan,size,weather:versionKey(payload,version),endpoints:endpoints(payload)})
      bytes+=size
      while (entries.size>maxEntries || bytes>maxBytes) {
        const first=entries.keys().next().value
        bytes-=entries.get(first).size;entries.delete(first)
      }
    },
    seeds(payload,version) {
      const weather=versionKey(payload,version),points=endpoints(payload),seen=new Set(),result=[]
      for (const entry of [...entries.values()].reverse()) {
        if (entry.weather!==weather || entry.endpoints!==points || entry.plan.points.length>2048) continue
        if (payload.prefer_simple_route && !isDisplayableRoute(entry.plan)) continue
        // A first-solution run must search the selected strategy/algorithm;
        // an unrelated cached route would otherwise end that search at once.
        if ((payload.stop_on_first_feasible || payload.optimize_during_display) && (entry.plan.planning_strategy!==payload.planning_strategy
          || entry.plan.planner_type!==payload.planner_type)) continue
        const geometry=canonical(entry.plan.points)
        if (!seen.has(geometry)) { seen.add(geometry);result.push(clone(entry.plan.points)) }
        if (result.length===4) break
      }
      return result
    },
    stats:() => ({entries:entries.size,bytes,epoch}),
    clear,
    dispose(){clear();target?.removeEventListener('pagehide',clear)},
  }
}

export function createPagePlanningGate({owner,cancelPage,wait=ms=>new Promise(resolve=>setTimeout(resolve,ms)),now=Date.now}={}) {
  let active
  const stop=ticket => {
    if (ticket.stopping) return ticket.stopping
    ticket.stopping=(async()=>{
      if (!ticket.client) return
      const until=now()+15000
      for (;;) {
        const state=await cancelPage(ticket.client,ticket.sequence)
        if (state.compute_finished) return
        if (now()>=until) throw new Error('上次规划计算尚未结束，请稍后重试')
        await wait(75)
      }
    })()
    return ticket.stopping
  }
  return {
    async run(execute,{signal,onWaiting}={}) {
      const previous=active
      const ticket={controller:new AbortController(),client:owner?.clientId,sequence:owner?.nextSequence?.()}
      const abort=()=>{ticket.controller.abort();void stop(ticket).catch(()=>{})}
      signal?.addEventListener('abort',abort,{once:true})
      if (signal?.aborted) abort()
      active=ticket
      try {
        if (previous) {
          previous.controller.abort()
          onWaiting?.()
          await stop(previous)
        }
        if (ticket.controller.signal.aborted) throw abortError()
        const result=await execute({signal:ticket.controller.signal,client_instance_id:ticket.client,
          client_request_sequence:ticket.sequence})
        if (ticket.controller.signal.aborted) throw abortError()
        return result
      } catch (reason) {
        if (ticket.controller.signal.aborted) throw abortError()
        throw reason
      } finally {
        signal?.removeEventListener('abort',abort)
        try { await stop(ticket) } finally { if (active===ticket) active=undefined }
      }
    },
  }
}
