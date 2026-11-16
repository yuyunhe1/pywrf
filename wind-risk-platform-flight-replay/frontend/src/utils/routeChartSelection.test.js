import assert from 'node:assert/strict'
import test from 'node:test'
import * as echarts from 'echarts'
import { getRouteChartSample, getWindChartSampleIndex } from './routeChartSelection.js'

const samples = [
  { distance_km: 0, wind_speed: 1, headwind_component: 0.5, lon: 118.6, lat: 31.1 },
  { distance_km: 0.03, wind_speed: 2, headwind_component: -1, lon: 118.61, lat: 31.11 },
  { distance_km: 0.06, wind_speed: 3, headwind_component: 2, lon: 118.62, lat: 31.12 },
]

const createChart = () => {
  const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 480, height: 220 })
  chart.setOption({
    animation: false,
    grid: { left: 40, right: 40, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: samples.map((sample) => sample.distance_km.toFixed(1)) },
    yAxis: [{ type: 'value' }, { type: 'value' }],
    series: [
      { type: 'line', showSymbol: false, data: samples.map((sample) => sample.wind_speed) },
      { type: 'bar', yAxisIndex: 1, data: samples.map((sample) => sample.headwind_component) },
    ],
  })
  return chart
}

test('timed profile clicks use actual distance with unevenly spaced samples', () => {
  const timed = [samples[0], { ...samples[1], distance_km: .001 }, { ...samples[2], distance_km: 10 }]
  const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 480, height: 220 })
  chart.setOption({ animation: false, grid: { left: 40, right: 40, top: 40, bottom: 30 },
    xAxis: { type: 'value', min: 0, max: 10 }, yAxis: { type: 'value', min: 0, max: 4 },
    series: [{ type: 'line', data: timed.map((sample) => [sample.distance_km, sample.wind_speed]) }],
  })
  try {
    for (const [distance, expected] of [[.001, 1], [9.8, 2], [-1, null]]) {
      const [offsetX, offsetY] = chart.convertToPixel({ seriesIndex: 0 }, [distance, 2])
      assert.equal(getWindChartSampleIndex(chart, { offsetX, offsetY }, timed, { distanceAxis: true }), expected)
    }
  } finally { chart.dispose() }
})

const click = (chart, index, seriesIndex = 0) => {
  const value = seriesIndex === 0 ? samples[index].wind_speed : samples[index].headwind_component
  const [offsetX, offsetY] = chart.convertToPixel({ seriesIndex }, [index, value])
  return { offsetX, offsetY }
}

test('actual ECharts pixel conversion distinguishes samples with identical rounded distance labels', () => {
  const chart = createChart()
  try {
    assert.equal(samples[0].distance_km.toFixed(1), samples[1].distance_km.toFixed(1))
    for (let index = 0; index < samples.length; index += 1) {
      const selected = getWindChartSampleIndex(chart, click(chart, index), samples)
      assert.equal(selected, index)
      assert.deepEqual(getRouteChartSample(samples, selected), samples[index])
    }
  } finally {
    chart.dispose()
  }
})

test('clicks on headwind bars and empty plot space select the corresponding route position', () => {
  const chart = createChart()
  try {
    assert.equal(getWindChartSampleIndex(chart, click(chart, 1, 1), samples), 1)
    const left = click(chart, 1)
    const right = click(chart, 2)
    const event = { offsetX: left.offsetX + (right.offsetX - left.offsetX) * 0.8, offsetY: 90 }
    assert.equal(getWindChartSampleIndex(chart, event, samples), 2)
  } finally {
    chart.dispose()
  }
})

test('legend and axis clicks outside the plot leave the selection unchanged', () => {
  const chart = createChart()
  try {
    for (const event of [{ offsetX: 200, offsetY: 10 }, { offsetX: 5, offsetY: 100 }, { offsetX: 200, offsetY: 215 }]) {
      assert.equal(getWindChartSampleIndex(chart, event, samples), null)
    }
    assert.equal(getWindChartSampleIndex(chart, { offsetX: NaN, offsetY: 90 }, samples), null)
    assert.equal(getWindChartSampleIndex(chart, click(chart, 1), []), null)
    assert.equal(getWindChartSampleIndex(chart, click(chart, 1), samples.map((sample) => ({ ...sample, lon: null }))), null)
  } finally {
    chart.dispose()
  }
})

test('cleared selections and invalid coordinates cannot produce map markers', () => {
  for (const index of [null, undefined, -1, 3, 0.5]) {
    assert.equal(getRouteChartSample(samples, index), null)
  }
  assert.equal(getRouteChartSample(undefined, 0), null)
  assert.equal(getRouteChartSample([{ lon: 118.6, lat: NaN }], 0), null)
  assert.deepEqual(getRouteChartSample([{ lon: 0, lat: 0 }], 0), { lon: 0, lat: 0 })
})
