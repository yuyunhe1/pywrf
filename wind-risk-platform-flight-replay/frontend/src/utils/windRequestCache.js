// Bound both retained frames and live requests. A prefetch leaves one network
// slot available for a visible frame; cancelling one reader never cancels another.
export const weatherFrameBytes = (value) => {
  if (Array.isArray(value)) return value.reduce((sum, frame) => sum + weatherFrameBytes(frame), 0)
  const arrays = [...(value?.velocity || []).map((item) => item.data), value?.wind_speed?.data].filter(Boolean)
  return 2048 + arrays.reduce((sum, array) => sum + array.length * 16, 0)
}

const aborted = () => new DOMException('风场加载已取消', 'AbortError')

export const createWindRequestCache = ({ maxBytes = 48 * 1024 * 1024, maxEntries = 24,
  concurrency = 2, maxQueued = 8, sizeOf = weatherFrameBytes } = {}) => {
  const values = new Map(), jobs = new Map()
  let bytes = 0, active = 0, background = 0, hits = 0, coalesced = 0, evictions = 0
  const remember = (key, value) => {
    const size = sizeOf(value)
    if (size > maxBytes || maxBytes <= 0 || maxEntries <= 0) return
    if (values.has(key)) bytes -= values.get(key).size
    values.delete(key)
    values.set(key, { value, size })
    bytes += size
    while (bytes > maxBytes || values.size > maxEntries) {
      const oldest = values.keys().next().value
      bytes -= values.get(oldest).size
      values.delete(oldest)
      evictions++
    }
  }
  const settle = (job, error, value) => {
    for (const reader of job.readers) {
      reader.detach()
      if (error) reader.reject(error)
      else reader.resolve(value)
    }
    job.readers.clear()
  }
  const cancel = (job) => {
    if (jobs.get(job.key) === job) jobs.delete(job.key)
    job.controller.abort()
    settle(job, aborted())
  }
  const pump = () => {
    while (active < concurrency) {
      const next = [...jobs.values()].filter((job) => !job.started && (job.priority > 0 || background < 1))
        .sort((a, b) => b.priority - a.priority)[0]
      if (!next) return
      next.started = true
      const speculative = next.priority <= 0
      active++
      if (speculative) background++
      Promise.resolve().then(() => {
        if (next.controller.signal.aborted) throw aborted()
        return next.load(next.controller.signal)
      }).then((value) => {
        if (!next.controller.signal.aborted && jobs.get(next.key) === next) {
          jobs.delete(next.key)
          remember(next.key, value)
          settle(next, null, value)
        }
      }, (error) => {
        if (jobs.get(next.key) === next) jobs.delete(next.key)
        settle(next, error)
      }).finally(() => {
        if (jobs.get(next.key) === next) jobs.delete(next.key)
        active--
        if (speculative) background--
        pump()
      })
    }
  }
  const get = (key, load, { signal, priority = 1 } = {}) => {
    if (signal?.aborted) return Promise.reject(aborted())
    if (values.has(key)) {
      const item = values.get(key)
      values.delete(key); values.set(key, item)
      hits++
      return Promise.resolve(item.value)
    }
    let job = jobs.get(key)
    if (job) {
      coalesced++
      job.priority = Math.max(job.priority, priority)
    } else {
      const queued = [...jobs.values()].filter((item) => !item.started)
      if (queued.length >= maxQueued) {
        const speculative = queued.find((item) => item.priority <= 0)
        if (priority > 0 && speculative) cancel(speculative)
        else return Promise.reject(new Error('风场加载队列已满，请稍后重试'))
      }
      job = { key, load, priority, controller: new AbortController(), readers: new Set(), started: false }
      jobs.set(key, job)
    }
    const result = new Promise((resolve, reject) => {
      const reader = { resolve, reject, detach: () => signal?.removeEventListener('abort', stop) }
      const stop = () => {
        reader.detach()
        job.readers.delete(reader)
        reject(aborted())
        if (!job.readers.size) cancel(job)
        pump()
      }
      job.readers.add(reader)
      signal?.addEventListener('abort', stop, { once: true })
    })
    pump()
    return result
  }
  return {
    get,
    prime: remember,
    peek(key) {
      const entry = values.get(key)
      if (!entry) return undefined
      values.delete(key); values.set(key, entry)
      return entry.value
    },
    // An immutable dataset can keep cached frames across profile refreshes;
    // changed source revisions explicitly invalidate only snapshot requests.
    invalidate(matches = () => true) {
      for (const [key, item] of values) if (matches(key)) { values.delete(key); bytes -= item.size }
      for (const job of [...jobs.values()]) if (matches(job.key)) cancel(job)
      pump()
    },
    stats: () => ({ entries: values.size, bytes, maxBytes, active, queued: [...jobs.values()].filter((job) => !job.started).length,
      background, hits, coalesced, evictions }),
  }
}
