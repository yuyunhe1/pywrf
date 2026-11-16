const terminal = new Set(['completed', 'timed_out', 'failed', 'cancelled'])

const cancelled = () => Object.assign(new Error('通航窗口评估已取消'), { name: 'AbortError' })
const delay = (milliseconds, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) return reject(cancelled())
  const abort = () => { clearTimeout(timer); reject(cancelled()) }
  const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, milliseconds)
  signal?.addEventListener('abort', abort, { once: true })
})

export async function pollV3Windows({ create, get, cancel, signal, onProgress,
  wait = delay, now = Date.now, intervalMs = 1000 }) {
  let id, revision = -1, retained, finished = false
  const began = now()
  try {
    let state = await create(signal)
    id = state.job_id
    const deadline = began + ((state.timeout_seconds || 600) + 60) * 1000
    for (;;) {
      if (signal?.aborted) throw cancelled()
      if (state.result && state.revision > revision) {
        revision = state.revision
        retained = state.result
        onProgress?.({ ...retained, window_job_status: state.status, window_message: state.message })
      }
      if (terminal.has(state.status)) {
        finished = true
        if (state.status === 'cancelled') throw cancelled()
        if (!retained) throw new Error(state.message || '尚未确认可飞区间')
        return { ...retained, window_job_status: state.status, window_message: state.message }
      }
      if (now() >= deadline) {
        if (retained) return { ...retained, complete: false, coverage_incomplete: true,
          window_job_status: 'timed_out', window_message: '等待达到时限，其余时段未确认' }
        throw new Error('窗口计算达到时限，尚未确认可飞区间')
      }
      await wait(intervalMs, signal)
      if (signal?.aborted) throw cancelled()
      try { state = await get(id, revision, signal) } catch (reason) {
        if (signal?.aborted) throw cancelled()
        if (reason.response?.status < 500 && reason.response.status !== 429) throw reason
        state = { ...state, result: null }
      }
    }
  } finally {
    if (id && !finished) await Promise.resolve().then(() => cancel(id)).catch(() => {})
  }
}

export const isV3Route = (route) => Boolean(route?.v3?.trajectory_id || route?.trajectory_id)

// A refresh of the same certificate must wait for the previous cancellation
// acknowledgement; otherwise server-side deduplication can attach it to the
// very job that the old request is about to cancel.
export const createWindowRequestCoordinator = () => {
  const active = new Map()
  return async (key, signal, compute) => {
    const previous = active.get(key)
    if (previous?.signal?.aborted) await previous.promise.catch(() => {})
    if (signal?.aborted) throw cancelled()
    const entry = { signal, promise: Promise.resolve().then(compute) }
    active.set(key, entry)
    try { return await entry.promise } finally {
      if (active.get(key) === entry) active.delete(key)
    }
  }
}
