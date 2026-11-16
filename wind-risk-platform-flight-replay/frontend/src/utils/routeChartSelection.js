export const getRouteChartSample = (samples, index) => {
  if (!Array.isArray(samples) || !Number.isInteger(index) || index < 0 || index >= samples.length) return null
  const sample = samples[index]
  if (!sample || !Number.isFinite(sample.lon) || !Number.isFinite(sample.lat)) return null
  return sample
}

export const getWindChartSampleIndex = (chart, event, samples, { distanceAxis = false } = {}) => {
  if (!chart || !Array.isArray(samples) || !samples.length) return null
  const pixel = [event.offsetX, event.offsetY]
  if (!pixel.every(Number.isFinite) || !chart.containPixel({ gridIndex: 0 }, pixel)) return null
  // Use the category index, not the rounded distance label: adjacent samples
  // can display the same kilometre label while representing different points.
  const category = chart.convertFromPixel({ xAxisIndex: 0 }, pixel[0])
  if (!Number.isFinite(category)) return null
  if (distanceAxis) {
    let low = 0, high = samples.length - 1
    while (low < high) {
      const middle = Math.floor((low + high) / 2)
      if (samples[middle].distance_km < category) low = middle + 1
      else high = middle
    }
    const previous = Math.max(0, low - 1)
    const index = Math.abs(samples[previous].distance_km - category) <= Math.abs(samples[low].distance_km - category) ? previous : low
    return getRouteChartSample(samples, index) ? index : null
  }
  const index = Math.max(0, Math.min(samples.length - 1, Math.round(category)))
  return getRouteChartSample(samples, index) ? index : null
}
