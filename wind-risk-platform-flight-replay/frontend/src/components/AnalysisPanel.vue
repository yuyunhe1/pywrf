<script setup>
import { durationLabel } from '../utils/routeRecords'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  buildHorizontalWindShearChartModel,
  formatMaxHorizontalWindShear,
} from '../utils/horizontalWindShearChart'
import { getRouteChartSample, getWindChartSampleIndex } from '../utils/routeChartSelection'
import { departureWindowDecision, emptyDepartureWindowMessage, formatDepartureTime } from '../utils/departureWindows'
import { formatReplayTime } from '../utils/flightReplay'
import RecommendationPanel from './RecommendationPanel.vue'

const props = defineProps({
  metadata: Object,
  routeActive: Boolean,
  planningStatus: Object,
  analysis: Object,
  thresholds: Object,
  selectedSampleIndex: { type: Number, default: null },
  departureWindows: Object,
  autoReplan: Boolean,
  replay: Object,
  playheadSampleIndex: { type: Number, default: null },
  recommendation: Object,
  recommendationPreview: String,
  recommendationSelected: String,
  recommendationPending: String,
  adoptingRecommendation: Boolean,
  recommending: Boolean,
})
const emit = defineEmits(['sample-select', 'departure-select', 'depart-now', 'window-cancel','recommendation-preview','recommendation-adopt','recommendation-close','recommendation-cancel','recommendation-select'])
let echarts, chartPromise, disposed = false
const chartLoading = ref(false), chartError = ref('')
const loadCharts = () => {
  if (echarts) return Promise.resolve(echarts)
  if (!chartPromise) {
    chartLoading.value = true; chartError.value = ''
    chartPromise = import('../utils/charts').then((module) => { echarts = module; return module })
      .catch(() => { chartError.value = '曲线加载失败，请重试'; return null })
      .finally(() => { chartLoading.value = false; chartPromise = undefined })
  }
  return chartPromise
}

const hasRoute = computed(() => Boolean(props.routeActive || props.replay?.active || props.analysis))
const windowLength = item => item.start === item.end ? '指定时刻起飞' : durationLabel((Date.parse(item.end) - Date.parse(item.start)) / 1000)
const numberLabel = (value, digits = 2) => Number.isFinite(value) ? value.toFixed(digits) : '—'
const flightDuration = computed(() => durationLabel(props.replay?.result?.flight_duration_seconds))
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
const currentDepartureDecision = computed(() => departureWindowDecision(props.departureWindows))
const flightMode = computed(() => props.replay?.active && props.replay.mode === 'flight')

const toBeijingTime = (utcText, bjText) => {
  if (bjText) return bjText
  if (!utcText) return '-'
  const date = new Date(utcText.includes('T') ? utcText : utcText.replace(' UTC', ':00Z').replace(' ', 'T'))
  if (!Number.isFinite(date.getTime())) return '-'
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

const selectWindChartSample = (event) => {
  const index = getWindChartSampleIndex(windChart, event, props.analysis?.samples, { distanceAxis: flightMode.value })
  if (index !== null) emit('sample-select', index)
}

const renderSelectedWindSample = () => {
  if (!windChart) return
  const index = props.selectedSampleIndex ?? (flightMode.value ? props.playheadSampleIndex : null)
  const sample = getRouteChartSample(props.analysis?.samples, index)
  const selected = sample && Number.isFinite(sample.wind_speed)
  const x = flightMode.value ? sample?.distance_km : index
  windChart.setOption({
    series: [{
      id: 'route-wind-speed',
      markPoint: {
        silent: true,
        symbol: 'circle',
        symbolSize: 10,
        itemStyle: { color: '#ff3b30', borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        data: selected ? [{ coord: [x, sample.wind_speed] }] : [],
      },
      markLine: {
        silent: true,
        symbol: 'none',
        lineStyle: { color: '#ff3b30', width: 1, type: 'dashed' },
        label: { show: false },
        data: selected ? [{ xAxis: x }] : [],
      },
    }],
  })
}

const renderWindChart = async () => {
  await nextTick()
  if (!windChartEl.value) { windChart?.dispose(); windChart = undefined; return }
  if (!await loadCharts() || disposed || !windChartEl.value) return
  if (windChart && windChart.getDom() !== windChartEl.value) { windChart.dispose(); windChart = undefined }
  if (!windChart) {
    windChart = echarts.init(windChartEl.value)
    // ZRender also receives clicks between hidden line symbols and on bars.
    windChart.getZr().on('click', selectWindChartSample)
  }
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
      textStyle: { color: '#8fa6b9', fontSize: 12 }
    },
    tooltip: {
      trigger: 'axis',
      confine: true,
      extraCssText: 'max-width: 250px; white-space: normal; line-height: 1.5;',
      backgroundColor: 'rgba(10, 25, 41, 0.9)',
      borderColor: 'rgba(68, 215, 182, 0.3)',
      textStyle: { color: '#eaf5ff', fontSize: 12 },
      axisPointer: { type: 'cross', lineStyle: { color: 'rgba(0, 243, 255, 0.3)' } },
      formatter: function (params) {
        if (!params.length) return '';
        const sample = samples[params[0].dataIndex];
        let html = `${Number(sample?.distance_km || 0).toFixed(3)} km<br/>`;
        if (flightMode.value && sample) {
          html += `位置：${sample.lon.toFixed(5)}, ${sample.lat.toFixed(5)}<br/>预计经过：${formatReplayTime(sample.eta)} 北京时间<br/>${sample.risk} · ${sample.reason}<br/>`;
        }

        params.forEach(param => {
          if (param.seriesName === '总风速') {
            html += `<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:#00f3ff;"></span> 风速: <b>${Number.isFinite(sample?.wind_speed) ? sample.wind_speed.toFixed(2) : '数据不足'}</b> m/s<br/>`;
          } else if (param.seriesName === '顺逆风分量' && sample && Number.isFinite(sample.headwind_component)) {
            const isTailwind = sample.headwind_component >= 0;
            const windComponentColor = isTailwind ? '#28c76f' : '#ea5455';
            const windComponentLabel = isTailwind ? '顺风分量' : '逆风分量';
            html += `<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:${windComponentColor};"></span> ${windComponentLabel}: <b>${sample.headwind_component > 0 ? '+' : ''}${sample.headwind_component.toFixed(2)}</b> m/s`;
          }
        });
        return html;
      }
    },
    xAxis: {
      type: flightMode.value ? 'value' : 'category',
      name: 'km',
      data: flightMode.value ? undefined : samples.map((item) => item.distance_km.toFixed(1)),
      min: flightMode.value ? 0 : undefined,
      max: flightMode.value ? samples.at(-1)?.distance_km || 1 : undefined,
      axisLabel: { color: '#8fa6b9', fontSize: 12 },
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.1)' } },
      splitLine: { show: false },
      nameTextStyle: { color: '#6b8296', fontSize: 12 }
    },
    yAxis: [
      {
        type: 'value',
        name: 'm/s',
        min: 0,
        max: ({ max }) => Number.isFinite(max) ? Math.max(1, Math.ceil(max * 1.15)) : 1,
        axisLabel: { color: '#8fa6b9', fontSize: 12 },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,0.05)', type: 'dashed' } },
        nameTextStyle: { color: '#6b8296', fontSize: 12 }
      },
      {
        type: 'value',
        name: 'm/s',
        position: 'right',
        axisLabel: { color: '#8fa6b9', fontSize: 12 },
        splitLine: { show: false },
        nameTextStyle: { color: '#6b8296', fontSize: 12 }
      }
    ],
    series: [
      {
        id: 'route-wind-speed',
        name: '总风速',
        type: 'line',
        smooth: flightMode.value ? false : 0.3,
        connectNulls: false,
        showSymbol: false,
        data: samples.map((item) => {
          const speed = Number.isFinite(item.wind_speed) ? Number(item.wind_speed.toFixed(3)) : null
          return flightMode.value ? [item.distance_km, speed] : speed
        }),
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
        data: samples.map((item) => flightMode.value ? [item.distance_km, item.headwind_component] : item.headwind_component),
        itemStyle: {
          color: function (params) {
            if ((Array.isArray(params.data) ? params.data[1] : params.data) > 0) {
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
        barWidth: flightMode.value ? 2 : '40%'
      }
    ],
  }, { notMerge: true })
  renderSelectedWindSample()
  windChart.resize()
}

const renderShearChart = async () => {
  await nextTick()
  if (shearChartStatus.value !== 'ready' || !shearChartEl.value) {
    shearChart?.dispose()
    shearChart = undefined
    return
  }
  if (!await loadCharts() || disposed || !shearChartEl.value) return

  const model = shearChartModel.value
  if (shearChart && shearChart.getDom() !== shearChartEl.value) { shearChart.dispose(); shearChart = undefined }
  shearChart ||= echarts.init(shearChartEl.value)
  shearChart.setOption({
    animationDuration: 250,
    grid: { left: 46, right: 18, top: 28, bottom: 34, containLabel: false },
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(10, 25, 41, 0.96)',
      borderColor: 'rgba(68, 215, 182, 0.35)',
      textStyle: { color: '#eaf5ff', fontSize: 12 },
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
      axisLabel: { color: '#8fa6b9', fontSize: 12 },
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.14)' } },
      splitLine: { show: false },
      nameTextStyle: { color: '#6b8296', fontSize: 12 },
    },
    yAxis: {
      type: 'value',
      min: 0,
      name: 'm/s',
      axisLabel: { color: '#8fa6b9', fontSize: 12 },
      axisLine: { show: true, lineStyle: { color: 'rgba(255,255,255,0.14)' } },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)', type: 'dashed' } },
      nameTextStyle: { color: '#6b8296', fontSize: 12 },
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
          fontSize: 12,
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

watch(() => [props.analysis, hasRoute.value], () => {
  renderWindChart()
  renderShearChart()
}, { deep: true })
watch(() => props.thresholds?.maxHorizontalWindShear, renderShearChart)
watch(() => props.selectedSampleIndex, renderSelectedWindSample)
watch(() => props.playheadSampleIndex, renderSelectedWindSample)
onMounted(() => {
  renderWindChart()
  renderShearChart()
  if (typeof ResizeObserver !== 'undefined' && panelEl.value) {
    resizeObserver = new ResizeObserver(resizeCharts)
    resizeObserver.observe(panelEl.value)
  }
})
onBeforeUnmount(() => {
  disposed = true
  resizeObserver?.disconnect()
  windChart?.getZr().off('click', selectWindChartSample)
  windChart?.dispose()
  shearChart?.dispose()
})
</script>

<template>
  <div ref="panelEl" class="panel-body analysis-body">
    <div class="panel-content">
      <RecommendationPanel :workflow="recommendation" :preview-id="recommendationPreview" :adopting="adoptingRecommendation" :busy="recommending"
        :selected-id="recommendationSelected" :pending-id="recommendationPending" @select="$emit('recommendation-select',$event)"
        @preview="$emit('recommendation-preview',$event)" @adopt="$emit('recommendation-adopt',$event)"
        @close-preview="$emit('recommendation-close')" @cancel="$emit('recommendation-cancel')" />
      <div v-if="!hasRoute" class="analysis-empty">
        <div class="assessment-illustration" aria-hidden="true">
          <svg viewBox="0 0 160 160" fill="none">
            <circle cx="80" cy="80" r="65" class="radar-outer" />
            <circle cx="80" cy="80" r="46" class="radar-ring" />
            <circle cx="80" cy="80" r="26" class="radar-ring" />
            <path d="M15 80h130M80 15v130" class="radar-axis" />
            <path d="M36 115 65 98l22-40 39-20" class="radar-route" />
            <circle cx="36" cy="115" r="4" class="radar-point" />
            <circle cx="126" cy="38" r="4" class="radar-point" />
            <path d="m81 60 13 29-15-7-14 9 16-31Z" class="radar-aircraft" />
            <path d="M49 15h14M15 49V35h14M111 145h-14M145 111v14h-14" class="radar-corners" />
          </svg>
        </div>
        <h2>{{ planningStatus?.label || '开始一次通航评估' }}</h2>
        <p>{{ planningStatus?.label ? '找到并验证航线后将在这里显示通航结果。' : '选择历史航线、在地图绘制，或设置起终点生成航线。' }}</p>
        <span v-if="planningStatus?.label && Number.isFinite(planningStatus.elapsed)">已用时 {{ Math.floor(planningStatus.elapsed) }} 秒</span>
      </div>
      <template v-else>
        <section v-if="departureWindows?.active" class="departure-windows" aria-live="polite">
          <h2>{{ departureWindows.result?.low_wind_certified ? '限定时间段内的低风窗口' : '未来可起飞窗口' }}</h2>
          <p class="current-departure" :style="{ color: currentDepartureDecision?.color }">现在起飞：{{ currentDepartureDecision?.level }}
            <span v-if="currentDepartureDecision?.level !== '允许通航'">{{ currentDepartureDecision?.message }}</span></p>
          <button v-if="departureWindows.loading && departureWindows.route?.v3 && !departureWindows.message" class="secondary" @click="$emit('window-cancel')">停止窗口计算</button>
          <template v-if="departureWindows.result && !departureWindows.error">
            <ul v-if="departureWindows.result.departure_windows.length" class="departure-window-list">
              <li v-for="(item, index) in departureWindows.result.departure_windows" :key="`${item.start}-${item.end}`" :class="{ 'nearest-window': index === 0 }">
                <span v-if="index === 0" class="window-badge">最近起飞窗口 · 北京时间</span>
                <strong>{{ formatDepartureTime(item.start) }}<template v-if="item.start !== item.end"> — {{ formatDepartureTime(item.end) }}</template><template v-else> 起飞</template></strong>
                <span class="window-duration">{{ item.start === item.end ? '指定时刻起飞' : `起飞窗口长度 ${windowLength(item)}` }}</span>
                <span>{{ item.arrival_bounds ? '预计到达范围' : '预计到达' }} {{ formatDepartureTime(item.first_arrival) }}<template v-if="item.start !== item.end"> — {{ formatDepartureTime(item.last_arrival) }}</template></span>
              </li>
            </ul>
            <p v-else class="empty-inline">{{ emptyDepartureWindowMessage(departureWindows.result.empty_reason) }}</p>
            <p v-if="departureWindows.result.coverage_incomplete" class="coverage-note">部分时段未确认</p>
            <details v-if="departureWindows.result.unconfirmed_ranges?.length" class="assessment-detail">
              <summary>未确认时段（{{ departureWindows.result.unconfirmed_ranges.length }} 段）</summary>
              <p v-for="(item, index) in departureWindows.result.unconfirmed_ranges.slice(0, 12)" :key="index">{{ formatDepartureTime(item.start) }} — {{ formatDepartureTime(item.end) }}<br>{{ item.reason }}</p>
            </details>
          </template>
          <p v-else class="empty-inline">{{ departureWindows.error || departureWindows.message || (departureWindows.loading ? '正在计算跨小时通航窗口…' : '等待有效数据后重新评估。') }}</p>
        </section>

        <section class="core-metrics-section">
          <h2>核心指标</h2>
          <div class="core-metrics">
            <div class="core-metric"><span :title="analysis?.peak_wind_model ? '包含斜穿、网格边界及角点接触，按实际经过时刻取全程最大值' : ''">{{ analysis?.peak_wind_model ? '全程峰值风速' : '最大风速' }}</span><strong>{{ numberLabel(analysis?.max_wind_speed) }}<small>m/s</small></strong></div>
            <div class="core-metric"><span>最大水平风切变</span><strong>{{ maxHorizontalWindShearDisplay.value }}<small v-if="maxHorizontalWindShearDisplay.unit">{{ maxHorizontalWindShearDisplay.unit }}</small></strong></div>
            <div class="core-metric"><span>航线里程</span><strong>{{ numberLabel(analysis?.total_distance_km, 1) }}<small>km</small></strong></div>
            <div class="core-metric"><span>预计航时</span><strong class="duration-value">{{ flightDuration }}</strong></div>
          </div>
        </section>
        <section class="wind-chart-section"><h2>航线风速与顺逆风</h2>
          <p v-if="chartLoading" role="status">正在加载曲线…</p>
          <p v-if="chartError" role="alert">{{ chartError }} <button @click="renderWindChart(); renderShearChart()">重试</button></p>
          <div ref="windChartEl" class="chart wind-chart"></div></section>
        <section class="shear-chart-section"><h2>航线水平风切变</h2>
          <div v-if="shearChartStatus === 'ready'" ref="shearChartEl" class="chart shear-chart"></div>
          <p v-else class="empty-inline">{{ shearChartMessage }}</p>
        </section>
        <details class="assessment-detail analysis-details">
          <summary>详细信息</summary>
          <dl class="detail-grid">
            <dt>平均风速</dt><dd>{{ numberLabel(analysis?.mean_wind_speed) }} m/s</dd>
            <dt>顺风占比</dt><dd>{{ numberLabel(analysis?.tailwind_ratio, 1) }}%</dd>
            <dt>高风险比例</dt><dd>{{ numberLabel(analysis?.danger_ratio, 1) }}%</dd>
            <dt>最长高风险航段</dt><dd>{{ numberLabel(analysis?.max_continuous_danger_km, 1) }} km</dd>
            <dt>综合等级</dt><dd :style="{ color: riskColorMap[analysis?.risk_level] || '#fff' }">{{ analysis?.risk_level || '—' }}</dd>
            <dt>风场时刻</dt><dd>{{ toBeijingTime(metadata?.valid_time, metadata?.valid_time_bj) }}</dd>
            <dt>起报时间</dt><dd>{{ toBeijingTime(metadata?.cycle, metadata?.cycle_bj) }}</dd>
            <dt>数据来源</dt><dd>{{ metadata?.source || '—' }}</dd>
            <dt>高度</dt><dd>{{ metadata?.level || replay?.result?.level || '—' }}</dd>
          </dl>
        </details>
      </template>
    </div>
  </div>
</template>
