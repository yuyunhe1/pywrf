// A page owns only jobs it created. Network messages keep background tabs alive;
// no visibility timer is used to decide whether a task should be cancelled.
export function createTaskOwner({ target = globalThis.window, Socket = globalThis.WebSocket,
  fetcher = globalThis.fetch, uuid = () => globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
  schedule = setTimeout, unschedule = clearTimeout } = {}) {
  let clientId = uuid(), leaving = false, sequence=0
  const jobs = new Map()
  const cancelKeepalive = (id, base = jobs.get(id)?.base || '/api/route/plan/jobs') => {
    try { Promise.resolve(fetcher(`${base}/${encodeURIComponent(id)}`, { method: 'DELETE', keepalive: true })).catch(() => {}) } catch { /* server lease is the fallback */ }
  }
  const detach = (id) => {
    const state = jobs.get(id)
    if (!state) return
    jobs.delete(id)
    unschedule(state.timer)
    state.socket?.close()
  }
  const attach = (id, base = '/api/route/plan/jobs') => {
    if (leaving) { cancelKeepalive(id, base); return () => {} }
    const state = { retry: 500, base }
    const attachedClientId = clientId
    jobs.set(id, state)
    const connect = () => {
      if (jobs.get(id) !== state || leaving) return
      const protocol = target.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const socket = new Socket(`${protocol}//${target.location.host}${base}/${encodeURIComponent(id)}/owner`)
      state.socket = socket
      socket.onopen = () => {
        if (jobs.get(id) !== state || leaving) return
        state.retry = 500
        socket.send(JSON.stringify({ client_instance_id: attachedClientId }))
      }
      socket.onmessage = ({ data }) => {
        if (jobs.get(id) !== state || leaving) return
        try {
          const value = JSON.parse(data)
          if (value.type === 'heartbeat') socket.send(JSON.stringify({ type: 'ack', seq: value.seq }))
        } catch { /* malformed messages do not renew a lease */ }
      }
      socket.onclose = () => {
        if (jobs.get(id) !== state || leaving) return
        state.timer = schedule(connect, state.retry)
        state.retry = Math.min(5000, state.retry * 2)
      }
      socket.onerror = () => socket.close()
    }
    connect()
    return () => detach(id)
  }
  const leave = () => {
    leaving = true
    if (sequence) {
      try { Promise.resolve(fetcher(`/api/route/planning-clients/${encodeURIComponent(clientId)}?through=${sequence}&forget=true`,
        {method:'DELETE',keepalive:true})).catch(()=>{}) } catch { /* owner lease is the fallback */ }
    }
    for (const id of [...jobs.keys()]) { cancelKeepalive(id); detach(id) }
  }
  const resume = () => { if (leaving) { leaving = false; clientId = uuid();sequence=0 } }
  target?.addEventListener('pagehide', leave)
  target?.addEventListener('pageshow', resume)
  return { get clientId() { return clientId }, nextSequence:()=>++sequence, attach, leave,
    dispose() { leave(); target?.removeEventListener('pagehide', leave); target?.removeEventListener('pageshow', resume) } }
}

export const taskOwner = typeof window === 'undefined' ? null : createTaskOwner()
