const terminalStatuses = new Set(['completed', 'timed_out', 'cancelled', 'failed'])

export const isVerifiedRoute = (result) => !result?.time_aware || (
  !result.reference_route && result.points?.length >= 2 && result.analysis?.navigation_allowed === true
)

const abortError = () => Object.assign(new Error('规划已取消'), { name: 'AbortError' })

const delay = (milliseconds, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) return reject(abortError())
  const onAbort = () => {
    clearTimeout(timer)
    reject(abortError())
  }
  const timer = setTimeout(() => {
    signal?.removeEventListener('abort', onAbort)
    resolve()
  }, milliseconds)
  signal?.addEventListener('abort', onAbort, { once: true })
})

// Collect improvements continuously; publish one initial best and one final best.
export async function pollRouteJob({ create, get, cancel, signal, onProgress, onStatus, onSearchProgress, onCreated,onCandidate,hasInitialResult=false,
  timeoutMs = 660000, intervalMs = 750, initialDisplayDelayMs = 0, wait = delay, now = Date.now }) {
  let jobId
  let revision = -1
  let progressRevision = -1
  let best
  let diagnostic
  let terminal = false
  let initialPublished = hasInitialResult
  let lastPublished = hasInitialResult ? true : undefined
  let releaseOwner
  const started = now()
  let deadline = started + timeoutMs
  const publishBest = (state) => {
    if (!best || best === lastPublished) return
    lastPublished = best
    onSearchProgress?.(null)
    onProgress?.(best, state)
  }
  const publishInitial = (state) => {
    if (initialPublished || !best || now() < started + initialDisplayDelayMs) return
    initialPublished = true
    publishBest(state)
  }
  try {
    if (signal?.aborted) throw abortError()
    let state = await create(signal)
    jobId = state.job_id
    releaseOwner = onCreated?.(jobId)
    deadline = started + Math.max(timeoutMs, ((state.timeout_seconds || 600) + 60) * 1000)
    for (;;) {
      if (signal?.aborted) throw abortError()
      if (state.result && state.revision > revision) {
        revision = state.revision
        if (isVerifiedRoute(state.result)) {
          best = state.result
          onCandidate?.(best,state)
        } else diagnostic = state.result
      }
      onStatus?.(state)
      if (Number.isInteger(state.progress_revision) && state.progress_revision > progressRevision) {
        progressRevision = state.progress_revision
        if (!lastPublished) onSearchProgress?.(terminalStatuses.has(state.status) ? null : state.search_progress, state)
      }
      if (terminalStatuses.has(state.status)) {
        terminal = true
        if (state.status === 'cancelled') throw abortError()
        const result = best || diagnostic
        if (!result) throw new Error(state.message || '未找到可行航线')
        publishBest(state)
        return { ...result, planning_status: state.status,
          planning_message: state.status === 'completed' && result.planning_message ? result.planning_message : state.message }
      }
      publishInitial(state)
      if (now() >= deadline) {
        if (best) {
          publishBest({ ...state, status: 'timed_out' })
          return { ...best, planning_status: 'timed_out', planning_message: '等待达到时限，保留最佳可行航线' }
        }
        throw new Error('规划超时，尚未找到可行航线')
      }
      const previewRemaining = !initialPublished && best ? started + initialDisplayDelayMs - now() : Infinity
      await wait(Math.min(intervalMs, deadline - now(), previewRemaining > 0 ? previewRemaining : Infinity), signal)
      if (signal?.aborted) throw abortError()
      try {
        state = await get(jobId, revision, signal, progressRevision)
      } catch (reason) {
        if (signal?.aborted) throw abortError()
        // Brief network/proxy failures must not discard a visible route.
        if (reason.response && reason.response.status < 500 && reason.response.status !== 429) throw reason
        state = { ...state, result: null, message: '连接暂时中断，正在重试并保留已显示的航线' }
      }
    }
  } finally {
    releaseOwner?.()
    onSearchProgress?.(null)
    if (jobId && !terminal) await Promise.resolve().then(() => cancel(jobId)).catch(() => {})
  }
}
