<script setup>
import * as echarts from 'echarts'
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({
  metadata: Object,
  analysis: Object,
})

const chartEl = ref()
let chart

const toBeijingTime = (utcText, bjText) => {
  if (bjText) return bjText
  if (!utcText) return '-'
  const date = new Date(utcText.replace(' UTC', ':00Z').replace(' ', 'T'))
  return `${new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)} 北京时间`
}

const renderChart = async () => {
  await nextTick()
  if (!chartEl.value) return
  chart ||= echarts.init(chartEl.value)
  const samples = props.analysis?.samples || []
  chart.setOption({
    grid: { left: 42, right: 14, top: 24, bottom: 35 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', name: 'km', data: samples.map((item) => item.distance_km.toFixed(1)), axisLabel: { color: '#8fa6b9' } },
    yAxis: { type: 'value', name: 'm/s', axisLabel: { color: '#8fa6b9' }, splitLine: { lineStyle: { color: '#24384a' } } },
    series: [{ type: 'line', smooth: true, showSymbol: false, data: samples.map((item) => item.wind_speed.toFixed(2)), lineStyle: { color: '#44d7b6', width: 3 }, areaStyle: { color: 'rgba(68,215,182,.16)' } }],
  })
  chart.resize()
}

watch(() => props.analysis, renderChart, { deep: true })
onBeforeUnmount(() => chart?.dispose())
</script>

<template>
  <aside class="panel analysis-panel">
    <div>
      <p class="eyebrow">ROUTE ANALYSIS</p>
      <h2>航线风险分析</h2>
    </div>
    <section class="metadata">
      <div><span>起报时间（北京时间）</span><strong>{{ metadata?.cycle_bj || '待加载' }}</strong></div>
      <div><span>风场时刻（北京时间）</span><strong>{{ toBeijingTime(metadata?.valid_time, metadata?.valid_time_bj) }}</strong></div>
      <div><span>UTC 参考</span><strong>{{ metadata?.valid_time || '待加载' }}</strong></div>
      <div><span>预报时效</span><strong>F{{ String(metadata?.forecast_hour ?? 0).padStart(3, '0') }}</strong></div>
      <div><span>高度层</span><strong>{{ metadata?.level || '-' }}</strong></div>
    </section>

    <section class="stats">
      <div class="stat"><span>最大风速</span><strong>{{ analysis?.max_wind_speed ?? '-' }}</strong><small>m/s</small></div>
      <div class="stat"><span>平均风速</span><strong>{{ analysis?.mean_wind_speed ?? '-' }}</strong><small>m/s</small></div>
      <div class="stat"><span>高风险比例</span><strong>{{ analysis ? (analysis.danger_ratio * 100).toFixed(1) : '-' }}</strong><small>%</small></div>
      <div class="stat risk"><span>综合等级</span><strong>{{ analysis?.risk_level || '未分析' }}</strong></div>
    </section>

    <section>
      <h3>航线距离—风速曲线</h3>
      <div ref="chartEl" class="chart"></div>
    </section>

    <section class="legend">
      <h3>风险图例</h3>
      <div><i class="safe"></i>安全 <i class="notice"></i>注意 <i class="medium"></i>中等风险</div>
      <div><i class="warning"></i>高风险 <i class="danger"></i>危险 / 建议禁飞</div>
    </section>
  </aside>
</template>
