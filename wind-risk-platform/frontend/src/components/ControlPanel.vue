<script setup>
const dataSources = [
  { value: 'gfs', label: 'GFS 原始数据' },
  { value: 'wrf', label: 'WRF 降尺度' },
]

const props = defineProps({
  options: { type: Object, required: true },
  selection: { type: Object, required: true },
  layers: { type: Object, required: true },
  thresholds: { type: Object, required: true },
  loading: Boolean,
})

defineEmits(['reload', 'clear-route'])
</script>

<template>
  <aside class="panel control-panel">
    <div>
      <p class="eyebrow">GFS LOW-ALTITUDE WIND</p>
      <h1>无人机航线风速风险平台</h1>
      <p class="subtle">{{ selection.source === 'wrf' ? '当前显示服务器 WRF d02 降尺度缓存风场。' : '默认显示中国区域 73°E–135°E、18°N–54°N 的 GFS 原始风场。' }}</p>
    </div>

    <section>
      <h2>数据选择</h2>
      <label>数据源<select v-model="selection.source"><option v-for="item in dataSources" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
      <label>北京时间风场时刻<select v-model="selection.validTime" :disabled="!options.valid_times.length"><option v-for="item in options.valid_times" :key="`${item.label}-${item.cycle}`" :value="item.label">{{ item.label }}</option></select></label>
      <p v-if="options.valid_times.length" class="subtle">对应起报：{{ selection.cycle }}，预报时效 F{{ String(selection.forecastHour || 0).padStart(3, '0') }}</p>
      <p v-else class="subtle">当前没有当前整点或未来的风场时刻，请下载新的 GFS 数据。</p>
      <label>高度层<select v-model="selection.level"><option v-for="level in options.levels" :key="level">{{ level }}</option></select></label>
      <button class="primary" :disabled="loading || !options.valid_times.length" @click="$emit('reload')">{{ loading ? '加载中...' : '加载当前风场' }}</button>
    </section>

    <section>
      <h2>图层显示</h2>
      <label class="switch"><input v-model="layers.heatmap" type="checkbox" /> 风速热力图</label>
      <label class="switch"><input v-model="layers.velocity" type="checkbox" /> 风向粒子流</label>
      <label class="switch"><input v-model="layers.route" type="checkbox" /> 航线与风险分段</label>
    </section>

    <section>
      <h2>风险阈值 <span>m/s</span></h2>
      <div class="threshold-grid">
        <label>安全<input v-model.number="thresholds.safe" type="number" min="0" step="0.5" /></label>
        <label>注意<input v-model.number="thresholds.notice" type="number" min="0" step="0.5" /></label>
        <label>预警<input v-model.number="thresholds.warning" type="number" min="0" step="0.5" /></label>
        <label>危险<input v-model.number="thresholds.danger" type="number" min="0" step="0.5" /></label>
      </div>
    </section>

    <section>
      <h2>航线操作</h2>
      <p class="subtle">点击地图左上角折线工具绘制航线，结束后自动分析。</p>
      <button class="ghost" @click="$emit('clear-route')">清除航线</button>
    </section>
  </aside>
</template>
