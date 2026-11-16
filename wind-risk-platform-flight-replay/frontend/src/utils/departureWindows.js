export const departureWindowDecision = (state) => {
  if (!state?.active) return null
  if (state.loading && state.result?.time_model !== 'constant_airspeed') return { level: '待评估', color: '#00f3ff', message: '正在评估现在起飞的完整航程和未来通航窗口…' }
  if (state.error) return { level: '待评估', color: '#f4c95d', message: state.error }
  const current = state.result?.current_departure
  if (!current) return { level: '待评估', color: '#00f3ff', message: '等待固定航线的跨时刻评估。' }
  const status = {
    allowed: { level: '允许通航', color: '#28c76f' },
    blocked: { level: '禁止通航', color: '#ea5455' },
    unknown: { level: '数据不足', color: '#f4c95d' },
  }[current.status] || { level: '数据不足', color: '#f4c95d' }
  return { ...status, message: current.reason }
}

export const formatDepartureTime = (value) => {
  const date = new Date(value)
  if (!value || !Number.isFinite(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(date)
}

export const emptyDepartureWindowMessage = (reason) => ({
  no_low_wind_window: '限定时间段内尚无已确认的连续低风窗口；可查看已验收的指定起飞时刻。',
  data_insufficient: '预报未完整覆盖后续航程，暂不能给出可起飞窗口。',
  conditions_unsuitable: '已覆盖的未来航程均触发风速或水平风切变限制。',
  short_window: '可飞区间过短，未形成按 5 分钟精度显示的起飞窗口。',
}[reason] || '暂无可起飞窗口。')

export const createDepartureWindowEvaluator = ({ request, onChange, now = Date.now,
  setTimer = setTimeout, clearTimer = clearTimeout }) => {
  let route
  let configuration
  let generation = 0
  let controller
  let timer
  let disposed = false
  let latestState

  const cancel = () => {
    generation += 1
    controller?.abort()
    controller = undefined
    clearTimer(timer)
    timer = undefined
  }
  const emit = (state) => { latestState = { active: Boolean(route), route, loading: false, result: null, error: '', ...state }; onChange(latestState) }
  const valid = () => {
    const speed = configuration?.average_ground_speed_mps
    if (!route?.v3 && (typeof speed !== 'number' || !Number.isFinite(speed) || speed <= 0)) return '平均对地速度必须是大于 0 的有效数值。'
    const limits = Object.values(configuration.thresholds || {})
    const shear = configuration.wind_shear?.horizontal?.hard_delta_wind_vector_ms
    if (!limits.length || limits.some((value) => !Number.isFinite(value) || value < 0)
      || !Number.isFinite(shear) || shear <= 0) return '请输入有效的风速和水平风切变阈值。'
    return ''
  }
  const commit = (result, sentAt) => {
    const expiry = Date.parse(result.refresh_at)
    const evaluatedAt = Date.parse(result.evaluated_at)
    const remaining = Number.isFinite(evaluatedAt) ? expiry - evaluatedAt - Math.max(0, now() - sentAt) : expiry - now()
    const cap = route?.v3 ? 600000 : 60000
    const delay = Number.isFinite(remaining) ? Math.min(cap, remaining) : cap
    if (delay < 20) {
      emit({ loading: true })
      timer = setTimer(refresh, 100)
      return
    }
    emit({ result })
    timer = setTimer(refresh, delay)
  }
  const refresh = async () => {
    if (!route || disposed) return
    cancel()
    const error = valid()
    if (error) { emit({ error }); return }
    const currentGeneration = generation
    controller = new AbortController()
    const signal = controller.signal
    const sentAt = now()
    emit({ loading: true })
    try {
      const result = await request(route.ephemeral || route.v3 ? route : route.route_id, configuration, signal, (partial) => {
        if (disposed || signal.aborted || currentGeneration !== generation) return
        emit({ loading: true, result: partial })
      })
      if (disposed || signal.aborted || currentGeneration !== generation) return
      // Use the server's validity duration, conservatively subtracting the
      // request duration; a visitor's clock need not match the backend clock.
      commit(result, sentAt)
    } catch (reason) {
      if (disposed || signal.aborted || currentGeneration !== generation) return
      const detail = reason.response?.data?.detail
      const message = typeof detail === 'string' ? detail : reason.message || '通航窗口评估暂未成功，稍后自动重试。'
      emit({ error: message })
      timer = setTimer(refresh, 60000)
    }
  }
  return {
    select(nextRoute, config, initialResult) {
      cancel()
      route = nextRoute
      configuration = config
      if (initialResult && !valid()) {
        commit(initialResult, now())
        return
      }
      return refresh()
    },
    update(config) {
      configuration = config
      if (!route || disposed) return
      cancel()
      emit({ loading: true })
      timer = setTimer(refresh, 300)
    },
    refresh,
    stop() { cancel(); emit({ ...latestState, loading: false, message: '窗口计算已停止，保留已确认区间。' }) },
    clear() { cancel(); route = undefined; emit({}) },
    dispose() { disposed = true; cancel(); route = undefined },
  }
}
