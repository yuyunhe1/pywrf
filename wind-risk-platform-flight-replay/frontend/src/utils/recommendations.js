const terminal = new Set(['completed', 'cancelled', 'failed', 'timed_out'])
export const RECOMMENDATION_COLORS = ['#ffb347', '#c084fc', '#34d399']
export const workflowLabel = stage => ({ preparing: '准备同批次风场', specified_search: '搜索指定起飞时刻',
  range_search:'在限定时间段内比较低风短航线',
  specified_improving: '比较已验收候选',
  specified_finishing: '首条可行路线已验收，结束搜索',
  future_search: '推荐未来起飞时间', baseline_windows: '评估原路径窗口', detour_search: '比较局部绕行方案',
  data_insufficient: '同批次风场数据不足',
  resource_limit: '资源受限，保留已确认方案', forecast_changed: '预报已更新，请重新推荐' }[stage])

const abortError = () => Object.assign(new Error('推荐已取消'), { name: 'AbortError' })
const pause = (ms, signal) => new Promise((resolve, reject) => {
  const stop = () => { clearTimeout(timer); reject(abortError()) }
  const timer = setTimeout(() => { signal?.removeEventListener('abort', stop); resolve() }, ms)
  signal?.addEventListener('abort', stop, { once: true })
  if (signal?.aborted) stop()
})

export async function pollRecommendationJob({ create, get, cancel, onCreated, signal, onStatus, onWorkflow,onCandidate,
  hasInitialResult=false,onProgress, onSearchProgress, wait = pause, now = Date.now, intervalMs = 750, initialDisplayDelayMs = 30000 }) {
  let id, release, done = false, result, workflow, routeRevision = -1, workflowRevision = -1, progressRevision = -1
  let displayed=hasInitialResult ? true : undefined, initialPublished = hasInitialResult
  const started = now()
  const publish = () => {
    if (!result || displayed === result) return
    displayed = result
    onSearchProgress?.(null)
    onProgress?.(result)
  }
  try {
    if (signal?.aborted) throw abortError()
    let state = await create(signal)
    id = state.job_id
    release = onCreated?.(id)
    const deadline = started + ((state.timeout_seconds || 7200) + 120) * 1000
    for (;;) {
      if (signal?.aborted) throw abortError()
      const forecastChanged = (state.workflow && state.workflow_revision > workflowRevision ? state.workflow : workflow)?.stage === 'forecast_changed'
      if (state.result_expired || forecastChanged) { result=undefined; displayed=undefined }
      if (state.revision > routeRevision) {
        routeRevision = state.revision
        if (!forecastChanged && !state.result_expired && state.result?.analysis?.navigation_allowed && state.result.points?.length >= 2) {
          result = state.result
          onCandidate?.(result,state)
        }
      }
      if (state.workflow && state.workflow_revision > workflowRevision) {
        workflowRevision = state.workflow_revision
        workflow = state.workflow
        onWorkflow?.(workflow)
      }
      onStatus?.({ ...state, workflow })
      if (!displayed && state.progress_revision > progressRevision) {
        progressRevision = state.progress_revision
        onSearchProgress?.(terminal.has(state.status) ? null : state.search_progress)
      }
      if (terminal.has(state.status)) {
        done = true
        if (state.status === 'cancelled') throw abortError()
        publish()
        if (state.status === 'failed' && !result && !workflow?.recommendations?.length) throw new Error(state.message)
        return { result, workflow, status: state.status, message: workflow?.message || state.message }
      }
      if (!initialPublished && result && now() - started >= initialDisplayDelayMs) { initialPublished = true; publish() }
      if (now() >= deadline) throw new Error('推荐等待达到时限，已确认方案保留')
      await wait(Math.min(intervalMs, result && !initialPublished ? Math.max(1, initialDisplayDelayMs - (now()-started)) : intervalMs), signal)
      try { state = await get(id, routeRevision, progressRevision, workflowRevision, signal) }
      catch (reason) {
        if (signal?.aborted) throw abortError()
        if (reason.response?.status && reason.response.status < 500 && reason.response.status !== 429) throw reason
        state = { ...state, result: null, workflow: null }
      }
    }
  } finally {
    release?.()
    onSearchProgress?.(null)
    if (id && !done) await Promise.resolve(cancel(id)).catch(() => {})
  }
}
