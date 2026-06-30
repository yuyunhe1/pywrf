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
  planner: { type: Object, required: true },
  savedRoutes: { type: Array, required: true },
  loading: Boolean,
  picking: { type: String, default: '' },
})

defineEmits(['reload', 'clear-route', 'pick-start', 'pick-end', 'plan-route', 'save-route', 'load-routes', 'delete-route'])
</script>

<template>
  <aside class="panel control-panel">
    <div class="panel-content">
      <div style="margin-bottom: 12px;">
        <p class="eyebrow">WIND RISK PLATFORM</p>
        <h1 style="font-size: 18px;">无人机风速预警</h1>
      </div>

      <section style="padding-top: 0;">
        <div class="grid-2">
          <label>数据源<select v-model="selection.source"><option v-for="item in dataSources" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
          <label>高度层<select v-model="selection.level"><option v-for="level in options.levels" :key="level">{{ level }}</option></select></label>
        </div>
        <label>风场时刻<select v-model="selection.validTime" :disabled="!options.valid_times.length"><option v-for="item in options.valid_times" :key="`${item.label}-${item.cycle}`" :value="item.label">{{ item.label }}</option></select></label>
        <button class="primary" :disabled="loading || !options.valid_times.length" @click="$emit('reload')" style="margin-top: 8px;">{{ loading ? '加载中...' : '加载当前风场' }}</button>
      </section>

      <section>
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <h2 style="margin:0">航线规划</h2>
          <button class="ghost" style="width: auto; padding: 2px 8px; font-size: 11px; margin: 0; color: #ff4757; border-color: rgba(255,71,87,0.3);" @click="$emit('clear-route')">清除航线</button>
        </div>
        <label>航线名称<input v-model="planner.name" type="text" placeholder="默认航线 A" /></label>
        <div style="display: grid; gap: 8px; margin-top: 8px;">
          <label>起点
            <div class="input-group">
              <input v-model="planner.startText" type="text" placeholder="经度, 纬度" />
              <button class="ghost" :class="{ 'active-pick': picking === 'start' }" @click="$emit('pick-start')" style="width: 60px;">{{ picking === 'start' ? '选点中' : '选点' }}</button>
            </div>
          </label>
          <label>终点
            <div class="input-group">
              <input v-model="planner.endText" type="text" placeholder="经度, 纬度" />
              <button class="ghost" :class="{ 'active-pick': picking === 'end' }" @click="$emit('pick-end')" style="width: 60px;">{{ picking === 'end' ? '选点中' : '选点' }}</button>
            </div>
          </label>
        </div>
        <div class="grid-2" style="margin-top: 10px;">
          <button class="ghost" :disabled="!planner.points.length" @click="$emit('save-route')" style="margin: 0;">保存航线</button>
          <button class="primary" :disabled="loading || !planner.startText || !planner.endText" @click="$emit('plan-route')" style="margin: 0;">
            <span v-if="planner.planning" class="loading-spinner"></span>
            生成规划航线
          </button>
        </div>
      </section>

      <section>
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <h2 style="margin:0">图层与风级阈值</h2>
          <label class="switch" style="margin:0; font-size: 11px;"><input v-model="layers.route" type="checkbox" /> 航线</label>
        </div>
        <div class="segmented-control" style="margin-bottom: 12px;">
          <button type="button" :class="{ active: layers.velocity }" @click="layers.velocity = true; layers.heatmap = false">风向粒子流</button>
          <button type="button" :class="{ active: layers.heatmap }" @click="layers.heatmap = true; layers.velocity = false">风速热力图</button>
        </div>
        <div class="threshold-grid">
          <label>一级风<input v-model.number="thresholds.safe" type="number" min="0" step="0.5" /></label>
          <label>二级风<input v-model.number="thresholds.notice" type="number" min="0" step="0.5" /></label>
          <label>三级风<input v-model.number="thresholds.warning" type="number" min="0" step="0.5" /></label>
          <label>四级风<input v-model.number="thresholds.danger" type="number" min="0" step="0.5" /></label>
        </div>
      </section>

      <section style="padding-bottom: 0; border-bottom: none;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <h2 style="margin:0">历史航线</h2>
          <button class="ghost" style="width: auto; padding: 2px 8px; font-size: 11px; margin: 0;" @click="$emit('load-routes')">刷新</button>
        </div>
        <div class="route-list">
          <div v-if="!savedRoutes.length" class="subtle" style="text-align: center; margin-top: 10px;">暂无保存的航线</div>
          <div v-for="route in savedRoutes" :key="route.route_id" class="saved-route">
            <button class="ghost" @click="$emit('plan-route', route)">{{ route.name }}</button>
            <button class="icon-button" title="删除" @click="$emit('delete-route', route.route_id)">×</button>
          </div>
        </div>
      </section>
    </div>
  </aside>
</template>
