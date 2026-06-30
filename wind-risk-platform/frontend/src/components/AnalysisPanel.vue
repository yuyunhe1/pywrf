<script setup>
import * as echarts from 'echarts'
import { nextTick, onBeforeUnmount, ref, watch, computed } from 'vue'

const props = defineProps({
  metadata: Object,
  analysis: Object,
  thresholds: Object,
})

const chartEl = ref()
const chartElCross = ref()
let chart
let chartCross

const riskColorMap = {
  '一级风': '#28c76f',
  '二级风': '#f4c95d',
  '三级风': '#ff9f43',
  '四级风': '#ea5455',
  '大于四级': '#9b51e0',
}

const riskStyle = computed(() => {
  const level = props.analysis?.risk_level
  const color = riskColorMap[level] || '#00f3ff' // 默认颜色 #00f3ff
  return {
    '--risk-color': color,
    borderColor: `${color}4D` // 30% 透明度
  }
})

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
      formatter: function(params) {
        if (!params.length) return '';
        const sample = samples[params[0].dataIndex];
        let html = `${params[0].axisValue} km<br/>`;
        
        params.forEach(param => {
          if (param.seriesName === '总风速') {
            html += `<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:#00f3ff;"></span> 风速: <b>${param.data}</b> m/s<br/>`;
          } else if (param.seriesName === '顺逆风分量' && sample && sample.headwind_component !== undefined) {
            const headwindColor = sample.headwind_component > 0 ? '#ea5455' : '#28c76f';
            html += `<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:${headwindColor};"></span> 逆风分量: <b>${sample.headwind_component > 0 ? '+' : ''}${sample.headwind_component}</b> m/s`;
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
          color: function(params) {
            if (params.data > 0) {
              return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: 'rgba(234, 84, 85, 0.8)' },
                { offset: 1, color: 'rgba(234, 84, 85, 0.0)' }
              ]);
            } else {
              return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: 'rgba(40, 199, 111, 0.0)' },
                { offset: 1, color: 'rgba(40, 199, 111, 0.8)' }
              ]);
            }
          },
          borderRadius: [3, 3, 3, 3]
        },
        barWidth: '40%'
      }
    ],
  })
  chart.resize()

  if (!chartElCross.value) return
  chartCross ||= echarts.init(chartElCross.value)
  
  chartCross.setOption({
    grid: { left: 35, right: 35, top: 30, bottom: 20 },
    legend: {
      data: [
        { name: '侧风分量', itemStyle: { color: '#ff9f43' } },
        { name: '航向角', itemStyle: { color: '#9b51e0' } },
        { name: '气象风向', itemStyle: { color: '#38bdf8' } }
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
      formatter: function(params) {
        if (!params.length) return '';
        const sample = samples[params[0].dataIndex];
        if (!sample) return '';
        let html = `${params[0].axisValue} km<br/>`;
        
        params.forEach(param => {
          if (param.seriesName === '侧风分量') {
            html += `<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:#ff9f43;"></span>侧风分量: <b>${param.data}</b> m/s<br/>`;
          } else if (param.seriesName === '航向角') {
            html += `<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:#9b51e0;"></span>航向角: <b>${param.data}°</b><br/>`;
          } else if (param.seriesName === '气象风向') {
            html += `<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:#38bdf8;"></span>气象风向: <b>${param.data.toFixed(0)}°</b>`;
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
        name: '°',
        position: 'right',
        min: 0,
        max: 360,
        interval: 90,
        axisLabel: { color: '#8fa6b9', fontSize: 9 },
        splitLine: { show: false },
        nameTextStyle: { color: '#6b8296', fontSize: 9 }
      }
    ],
    series: [
      { 
        type: 'line', 
        smooth: 0.3, 
        showSymbol: false, 
        name: '侧风分量',
        data: samples.map((item) => Math.abs(item.crosswind_component)), 
        lineStyle: { color: '#ff9f43', width: 2, shadowColor: 'rgba(255, 159, 67, 0.5)', shadowBlur: 10 }, 
        areaStyle: { 
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(255, 159, 67, 0.3)' },
            { offset: 1, color: 'rgba(255, 159, 67, 0.0)' }
          ])
        } 
      },
      {
        type: 'line',
        yAxisIndex: 1,
        smooth: false,
        showSymbol: false,
        name: '航向角',
        data: samples.map((item) => item.flight_heading),
        lineStyle: { color: '#9b51e0', width: 1.5, type: 'dashed' }
      },
      {
        type: 'line',
        yAxisIndex: 1,
        smooth: 0.2,
        showSymbol: false,
        name: '气象风向',
        data: samples.map((item) => item.wind_direction_from),
        lineStyle: { color: '#38bdf8', width: 1.5, type: 'dashed' }
      }
    ],
  })
  chartCross.resize()
}

watch(() => props.analysis, renderChart, { deep: true })
onBeforeUnmount(() => {
  chart?.dispose()
  chartCross?.dispose()
})
</script>

<template>
  <aside class="panel analysis-panel">
    <div class="panel-content">
      <div style="margin-bottom: 12px;">
        <p class="eyebrow">ROUTE ANALYSIS</p>
        <h1 style="font-size: 18px; margin-bottom: 0;">航线风险分析</h1>
      </div>
      
      <section class="metadata" style="padding-top: 0;">
        <div><span>风场时刻</span><strong>{{ toBeijingTime(metadata?.valid_time, metadata?.valid_time_bj) }}</strong></div>
        <div><span>起报时间</span><strong>{{ metadata?.cycle_bj || '待加载' }} (F{{ String(metadata?.forecast_hour ?? 0).padStart(3, '0') }})</strong></div>
        <div><span>高度层</span><strong>{{ metadata?.level || '-' }}</strong></div>
        <div v-if="analysis"><span>航线总里程</span><strong>{{ analysis.total_distance_km }} km</strong></div>
        <div v-if="analysis"><span>最长连续高风险里程</span><strong>{{ analysis.max_continuous_danger_km }} km</strong></div>
      </section>

      <section class="stats" style="grid-template-columns: 1fr 1fr; gap: 8px;">
        <div class="stat"><span>最大风速</span><strong>{{ analysis?.max_wind_speed ?? '-' }}<small>m/s</small></strong></div>
        <div class="stat"><span>平均风速</span><strong>{{ analysis?.mean_wind_speed ?? '-' }}<small>m/s</small></strong></div>
        <div class="stat"><span>高风险比例</span><strong>{{ analysis ? (analysis.danger_ratio * 100).toFixed(1) : '-' }}<small>%</small></strong></div>
        <div class="stat"><span>顺风路段占比</span><strong>{{ analysis ? (analysis.tailwind_ratio * 100).toFixed(1) : '-' }}<small>%</small></strong></div>
      </section>
      
      <div class="stat risk" :style="riskStyle" style="margin-top: 8px; min-height: 50px; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;">
        <span style="color: #8099aa; font-size: 12px;">综合等级</span>
        <strong style="margin: 0; font-size: 20px;">{{ analysis?.risk_level || '-' }}</strong>
      </div>

      <section>
        <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 4px;">
          <h3 style="margin-bottom: 0;">航线风速与顺逆风分析</h3>
        </div>
        <p class="subtle" style="margin-top: 0; font-size: 10px; margin-bottom: 4px;">点击图例筛选；蓝色曲线代表总风速，底部柱状图代表顺风(绿)或逆风(红)分量。</p>
        <div ref="chartEl" class="chart"></div>
      </section>
      
      <section>
        <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 4px;">
          <h3 style="margin-bottom: 0;">航线侧风与风向分析</h3>
        </div>
        <p class="subtle" style="margin-top: 0; font-size: 10px; margin-bottom: 4px;">点击图例筛选；橙色曲线代表切变横风大小，虚线反映气象风向和无人机飞行航向。</p>
        <div ref="chartElCross" class="chart"></div>
      </section>
    </div>
  </aside>
</template>
