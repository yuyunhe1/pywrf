<script setup>
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  buildHorizontalWindShearChartModel,
  formatMaxHorizontalWindShear,
} from '../utils/horizontalWindShearChart'
import {
  buildNavigationDecision,
  hasHorizontalWindShearRisk,
} from '../utils/navigationDecision'

const props = defineProps({
  metadata: Object,
  analysis: Object,
  thresholds: Object,
})

const panelEl = ref()
const windChartEl = ref()
const shearChartEl = ref()
let windChart
let shearChart
let resizeObserver

const riskColorMap = {
  '一级风': '#28c76f',
  '二级风': '#f4c95d',
  '三级风': '#ff9f43',
  '四级风': '#ea5455',
  '大于四级': '#9b51e0'
}

const riskStyle = computed(() => {
  const level = props.analysis?.risk_level
  const color = riskColorMap[level] || '#00f3ff' // 默认颜色 #00f3ff
  return {
    '--risk-color': color,
    borderColor: `${color}4D` // 30% 透明度
  }
})

const horizontalWindShear = computed(() => props.analysis?.wind_shear)
const horizontalWindShearProfile = computed(() => horizontalWindShear.value?.horizontal_wind_shear_profile)
const shearPlanningBlocked = computed(() => Boolean(
  props.analysis?.wind_shear_fallback || props.analysis?.wind_shear_failure,
))
const shearChartModel = computed(() => buildHorizontalWindShearChartModel(
  horizontalWindShearProfile.value,
  props.thresholds?.maxHorizontalWindShear,
))
const shearChartStatus = computed(() => {
  if (shearPlanningBlocked.value) return 'blocked'
  if (!Array.isArray(horizontalWindShearProfile.value)) return 'missing'
  return shearChartModel.value.events.length > 0 ? 'ready' : 'no-events'
})
const shearChartMessage = computed(() => ({
  blocked: '存在超过水平风切变阈值的阻断航段，当前条件下未生成可行航线。',
  missing: '暂无水平风切变数据',
  'no-events': '该航线未检测到明显水平风切变',
}[shearChartStatus.value] || ''))
const maxHorizontalWindShearDisplay = computed(() => formatMaxHorizontalWindShear(
  horizontalWindShear.value?.max_horizontal_wind_shear,
  shearPlanningBlocked.value,
))
const horizontalWindShearRisk = computed(() => hasHorizontalWindShearRisk(props.analysis))
const horizontalWindShearRiskStyle = {
  '--risk-color': '#ea5455',
  borderColor: '#ea54554D',
}

const navigationDecision = computed(() => buildNavigationDecision(
  props.analysis,
  props.thresholds?.danger,
))

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

const renderWindChart = async () => {
  await nextTick()
  if (!windChartEl.value) return
  windChart ||= echarts.init(windChartEl.value)
  const samples = props.analysis?.samples || []
  windChart.setOption({
    grid: { left: 35, right: 35, top: 30, bottom: 20 },
    legend: {
      data: [
        { name: '总风速', itemStyle: { color: '#00f3ff' } },
        {
          name: '顺逆风分量',
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
              { offset: 0, color: '#28c76f' },
              { offset: 0.5, color: '#28c76f' },
              { offset: 0.5, color: '#ea5455' },
              { offset: 1, color: '#ea5455' }
            ])
          }
        }
      ],
      icon: 'circle',
      top: 0,
      left: 'center',
      itemWidth: 8,
      itemHeight: 8,
      textStyle: { color: '#8fa6b9', fontSize: 9 }
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(10, 25, 41, 0.9)',
      borderColor: 'rgba(68, 215, 182, 0.3)',
      textStyle: { color: '#eaf5ff', fontSize: 11 },
      axisPointer: { type: 'cross', lineStyle: { color: 'rgba(0, 243, 255, 0.3)' } },
      formatter: function (params) {
        if (!params.length) return '';
        const sample = samples[params[0].dataIndex];
        let html = `${params[0].axisValue} km<br/>`;

        params.forEach(param => {
          if (param.seriesName === '总风速') {
            html += `<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:#00f3ff;"></span> 风速: <b>${param.data}</b> m/s<br/>`;
          } else if (param.seriesName === '顺逆风分量' && sample && sample.headwind_component !== undefined) {
            const isTailwind = sample.headwind_component >= 0;
            const windComponentColor = isTailwind ? '#28c76f' : '#ea5455';
            const windComponentLabel = isTailwind ? '顺风分量' : '逆风分量';
            html += `<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:${windComponentColor};"></span> ${windComponentLabel}: <b>${sample.headwind_component > 0 ? '+' : ''}${sample.headwind_component}</b> m/s`;
          }
        });
        return html;
      }
    },
    xAxis: {
      type: 'category',
      name: 'km',
      data: samples.map((item) => item.distance_km.toFixed(1)),
      axisLabel: { color: '#8fa6b9', fontSize: 9 },
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.1)' } },
      nameTextStyle: { color: '#6b8296', fontSize: 9 }
    },
    yAxis: [
      {
        type: 'value',
        name: 'm/s',
        axisLabel: { color: '#8fa6b9', fontSize: 9 },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,0.05)', type: 'dashed' } },
        nameTextStyle: { color: '#6b8296', fontSize: 9 }
      },
      {
        type: 'value',
        name: 'm/s',
        position: 'right',
        axisLabel: { color: '#8fa6b9', fontSize: 9 },
        splitLine: { show: false },
        nameTextStyle: { color: '#6b8296', fontSize: 9 }
      }
    ],
    series: [
      {
        name: '总风速',
        type: 'line',
        smooth: 0.3,
        showSymbol: false,
        data: samples.map((item) => item.wind_speed.toFixed(2)),
        lineStyle: { color: '#00f3ff', width: 2, shadowColor: 'rgba(0, 243, 255, 0.5)', shadowBlur: 10 },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(0, 243, 255, 0.3)' },
            { offset: 1, color: 'rgba(0, 243, 255, 0.0)' }
          ])
        }
      },
      {
        name: '顺逆风分量',
        type: 'bar',
        yAxisIndex: 1,
        data: samples.map((item) => item.headwind_component),
        itemStyle: {
          color: function (params) {
            if (params.data > 0) {
              return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: 'rgba(40, 199, 111, 0.8)' },
                { offset: 1, color: 'rgba(40, 199, 111, 0.0)' }
              ]);
            } else {
              return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: 'rgba(234, 84, 85, 0.0)' },
                { offset: 1, color: 'rgba(234, 84, 85, 0.8)' }
              ]);
            }
          },
          borderRadius: [3, 3, 3, 3]
        },
        barWidth: '40%'
      }
    ],
  })
  windChart.resize()
}

const renderShearChart = async () => {
  await nextTick()
  if (shearChartStatus.value !== 'ready' || !shearChartEl.value) {
    shearChart?.dispose()
    shearChart = undefined
    return
  }

  const model = shearChartModel.value
  shearChart ||= echarts.init(shearChartEl.value)
  shearChart.setOption({
    animationDuration: 250,
    grid: { left: 46, right: 18, top: 28, bottom: 34, containLabel: false },
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(10, 25, 41, 0.96)',
      borderColor: 'rgba(68, 215, 182, 0.35)',
      textStyle: { color: '#eaf5ff', fontSize: 11 },
      formatter: ({ data }) => {
        const item = data?.profile
        if (!item) return ''
        return [
          `航线里程：${Number(item.start_distance_km).toFixed(1)}～${Number(item.end_distance_km).toFixed(1)} km`,
          `水平风切变：${Number(item.horizontal_wind_shear).toFixed(2)} m/s`,
          `风矢量变化：${Number(item.delta_wind_vector_ms).toFixed(2)} m/s`,
          `航段长度：${Number(item.segment_distance_km).toFixed(2)} km`,
        ].join('<br>')
      },
    },
    xAxis: {
      type: 'value',
      min: 0,
      max: model.routeDistanceKm > 0 ? model.routeDistanceKm : undefined,
      name: '累计里程 (km)',
      nameLocation: 'middle',
      nameGap: 24,
      axisLabel: { color: '#8fa6b9', fontSize: 9 },
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.14)' } },
      splitLine: { show: false },
      nameTextStyle: { color: '#6b8296', fontSize: 9 },
    },
    yAxis: {
      type: 'value',
      min: 0,
      name: 'm/s',
      axisLabel: { color: '#8fa6b9', fontSize: 9 },
      axisLine: { show: true, lineStyle: { color: 'rgba(255,255,255,0.14)' } },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)', type: 'dashed' } },
      nameTextStyle: { color: '#6b8296', fontSize: 9 },
    },
    series: [{
      name: '水平风切变',
      type: 'bar',
      data: model.barData,
      barWidth: 10,
      itemStyle: {
        color: '#00c2d7',
        borderColor: '#78f4ff',
        borderWidth: 1,
        borderRadius: [2, 2, 0, 0],
      },
      emphasis: { itemStyle: { color: '#44d7b6' } },
      markLine: model.threshold === null ? undefined : {
        silent: true,
        symbol: 'none',
        lineStyle: { color: '#ea5455', width: 1.5, type: 'dashed' },
        label: {
          show: true,
          formatter: `硬约束阈值 ${model.threshold.toFixed(2)}`,
          color: '#ea5455',
          fontSize: 9,
          position: 'insideEndTop',
        },
        data: [{ yAxis: model.threshold }],
      },
    }],
  }, true)
  shearChart.resize()
}

const resizeCharts = () => {
  windChart?.resize()
  shearChart?.resize()
}

watch(() => props.analysis, () => {
  renderWindChart()
  renderShearChart()
}, { deep: true })
watch(() => props.thresholds?.maxHorizontalWindShear, renderShearChart)
onMounted(() => {
  renderWindChart()
  renderShearChart()
  if (typeof ResizeObserver !== 'undefined' && panelEl.value) {
    resizeObserver = new ResizeObserver(resizeCharts)
    resizeObserver.observe(panelEl.value)
  }
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  windChart?.dispose()
  shearChart?.dispose()
})
</script>

<template>
  <aside ref="panelEl" class="panel analysis-panel">
    <div class="panel-content" style="padding-bottom: 4px;">
      <div style="margin-bottom: 8px;">
        <p class="eyebrow">ROUTE ANALYSIS</p>
        <h1 style="font-size: 18px; margin-bottom: 0;">航线风险分析</h1>
      </div>

      <section style="padding: 0; background: transparent; border: none; box-shadow: none; margin-bottom: 8px;">
        <div style="display: grid; grid-template-columns: 1fr; gap: 6px; margin-bottom: 6px;">
          <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.2); padding: 8px 12px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05);">
            <span style="color: #a1b8cb; font-size: 12px; font-weight: 600;">风场时刻</span>
            <strong style="color: #fff; font-size: 13px; font-family: monospace; white-space: nowrap;">{{ toBeijingTime(metadata?.valid_time, metadata?.valid_time_bj).replace(' 北京时间', '') }}</strong>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.2); padding: 8px 12px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05);">
            <span style="color: #a1b8cb; font-size: 12px; font-weight: 600; white-space: nowrap; margin-right: 4px;">起报时间</span>
            <strong style="color: #fff; font-size: 13px; font-family: monospace; white-space: nowrap;">{{ metadata?.cycle_bj ? metadata.cycle_bj.replace(' 北京时间', '') : '待加载' }} <span style="color: #8099aa; font-size: 11px;">(F{{ String(metadata?.forecast_hour ?? 0).padStart(3, '0') }})</span></strong>
          </div>
        </div>
        <div v-if="analysis" style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px;">
          <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.2); padding: 8px 12px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05);">
            <span style="color: #a1b8cb; font-size: 12px; font-weight: 600; white-space: nowrap; margin-right: 4px;">总里程</span>
            <strong style="color: #00f3ff; font-size: 14px; font-family: monospace; white-space: nowrap;">{{ analysis?.total_distance_km?.toFixed(1) ?? '-' }} <span style="font-size: 10px; color: #8099aa;">km</span></strong>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.2); padding: 8px 12px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05);">
            <span style="color: #a1b8cb; font-size: 12px; font-weight: 600; white-space: nowrap; margin-right: 4px;">最长高风险</span>
            <strong style="color: #ea5455; font-size: 14px; font-family: monospace; white-space: nowrap;">{{ analysis?.max_continuous_danger_km?.toFixed(1) ?? '-' }} <span style="font-size: 10px; color: #8099aa;">km</span></strong>
          </div>
        </div>
      </section>

      <section class="stats route-metrics" style="padding: 0; background: transparent; border: none; box-shadow: none; margin-bottom: 8px;">
        <div class="stat route-metric">
          <span class="route-metric-label">最大风速</span>
          <strong class="route-metric-value">{{ analysis?.max_wind_speed ?? '-' }}<small>m/s</small></strong>
        </div>
        <div class="stat route-metric">
          <span class="route-metric-label">最大风切变</span>
          <strong class="route-metric-value">{{ maxHorizontalWindShearDisplay.value }}<small v-if="maxHorizontalWindShearDisplay.unit">{{ maxHorizontalWindShearDisplay.unit }}</small></strong>
        </div>
        <div class="stat route-metric">
          <span class="route-metric-label">平均风速</span>
          <strong class="route-metric-value">{{ analysis?.mean_wind_speed ?? '-' }}<small>m/s</small></strong>
        </div>
        <div class="stat route-metric">
          <span class="route-metric-label">高风险比例</span>
          <strong class="route-metric-value">{{ analysis ? (analysis.danger_ratio * 100).toFixed(1) : '-' }}<small>%</small></strong>
        </div>
        <div class="stat route-metric">
          <span class="route-metric-label">顺风占比</span>
          <strong class="route-metric-value">{{ analysis ? (analysis.tailwind_ratio * 100).toFixed(1) : '-' }}<small>%</small></strong>
        </div>
      </section>

      <div class="stat risk" :style="riskStyle"
        style="margin-top: 0; margin-bottom: 8px; min-height: 44px; padding: 6px 14px; display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.2); border: 1px solid rgba(255,255,255,0.05); border-radius: 6px;">
        <span style="color: #8099aa; font-size: 12px;">综合等级</span>
        <strong style="margin: 0; font-size: 18px;">{{ analysis?.risk_level || '-' }}</strong>
      </div>

      <div v-if="horizontalWindShearRisk" class="stat risk" :style="horizontalWindShearRiskStyle"
        style="margin-top: 0; margin-bottom: 8px; min-height: 44px; padding: 6px 14px; display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.2); border-radius: 6px;">
        <span style="color: #8099aa; font-size: 12px;">风切变提示</span>
        <strong style="margin: 0; font-size: 15px;">存在水平风切变风险</strong>
      </div>

      <section style="padding: 10px 12px; margin-bottom: 8px;">
        <div class="decision-card" style="padding: 10px; background: rgba(0,0,0,0.2); border-radius: 6px;"
          :style="{ '--decision-color': navigationDecision.color, borderColor: `${navigationDecision.color}55`, boxShadow: `inset 0 0 18px ${navigationDecision.color}18` }">
          <div class="decision-head" style="margin-bottom: 4px;">
            <span class="decision-label">通航决策</span>
            <strong>{{ navigationDecision.level }}</strong>
          </div>
          <p class="subtle" style="margin: 0; font-size: 11px;">{{ navigationDecision.message }}</p>
        </div>
      </section>

      <section style="padding: 10px 12px; margin-bottom: 0;">
        <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 2px;">
          <h3 style="margin-bottom: 0;">航线风速与顺逆风</h3>
        </div>
        <p class="subtle" style="margin-top: 0; font-size: 10px; margin-bottom: 4px;">
          点击图例筛选；蓝线为总风速，底部柱状图为顺风(绿)或逆风(红)。</p>
        <div ref="windChartEl" class="chart" style="height: 140px; margin-top: 4px;"></div>
      </section>

      <section style="padding: 10px 12px; margin-bottom: 0;">
        <h3 style="margin-bottom: 6px;">航线水平风切变</h3>
        <div v-if="shearChartStatus === 'ready'" ref="shearChartEl" class="chart" style="height: 200px;"></div>
        <div v-else class="shear-chart-message">{{ shearChartMessage }}</div>
      </section>
    </div>
  </aside>
</template>

<style scoped>
.route-metrics {
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 4px;
}

.route-metric {
  min-width: 0;
  min-height: 48px;
  padding: 5px 1px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  background: rgba(0, 0, 0, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.05);
  border-radius: 6px;
}

.route-metric .route-metric-label {
  min-height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #a1b8cb;
  font-size: 9px;
  font-weight: 600;
  line-height: 1.15;
  white-space: normal;
}

.route-metric .route-metric-value {
  max-width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;
}

.route-metric .route-metric-value small {
  margin-left: 1px;
  font-size: 8px;
}

.shear-chart-message {
  min-height: 180px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 18px;
  color: #8fa6b9;
  font-size: 12px;
  line-height: 1.6;
  text-align: center;
  border: 1px dashed rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.14);
}
</style>
