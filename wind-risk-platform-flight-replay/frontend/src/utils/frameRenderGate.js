// A late canvas callback must never complete a newer frame's loading state.
export const createFrameRenderGate = (onReady) => {
  let current, required = [], completed = new Set(), announced = false
  return {
    reset(key, sides) {
      if (key === current && sides.join('|') === required.join('|')) return
      current = key; required = sides; completed = new Set(); announced = false
      if (!sides.length && key) { announced = true; onReady(key) }
    },
    ready(key, side) {
      if (key !== current || !key || announced) return
      completed.add(side)
      if (required.every((value) => completed.has(value))) { announced = true; onReady(key) }
    },
  }
}
