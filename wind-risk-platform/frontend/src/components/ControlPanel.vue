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
  areaSelection: { type: Object, required: true },
  areaPresets: { type: Object, required: true },
  loading: Boolean,
  picking: { type: String, default: '' },
})

const emit = defineEmits(['reload', 'clear-route', 'pick-start', 'pick-end', 'plan-route', 'save-route', 'load-routes', 'delete-route', 'focus-area'])

const handleAreaChange = (kind, value) => {
  emit('focus-area', { kind, value })
}

const handleSourceClear = () => {
  props.selection.source = ''
}

const handleLevelClear = () => {
  props.selection.level = ''
}

const handleValidTimeClear = () => {
  props.selection.validTime = ''
}

const formatRouteDate = (value) => {
  if (!value) return '未记录时间'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未记录时间'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}
</script>

<template>
  <aside class="panel control-panel">
    <div class="panel-content">
      <div style="margin-bottom: 12px;">
        <p class="eyebrow">FLIGHT PLANNING</p>
        <h1 style="font-size: 18px;">航线规划与配置</h1>
      </div>

      <section style="padding-top: 0;">
        <h2 style="margin: 0 0 8px;">数据选择</h2>
        <div class="grid-2">
          <label>数据源
            <el-select
              v-model="selection.source"
              class="glass-select"
              popper-class="glass-select-popper"
              filterable
              clearable
              placeholder="请选择数据源"
              @clear="handleSourceClear"
            >
              <el-option v-for="item in dataSources" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </label>
          <label>高度层
            <el-select
              v-model="selection.level"
              class="glass-select"
              popper-class="glass-select-popper"
              filterable
              clearable
              placeholder="请选择高度层"
              @clear="handleLevelClear"
            >
              <el-option v-for="level in options.levels" :key="level" :label="level" :value="level" />
            </el-select>
          </label>
        </div>
        <label>风场时刻
          <el-select
            v-model="selection.validTime"
            class="glass-select"
            popper-class="glass-select-popper"
            filterable
            clearable
            placeholder="请选择风场时刻"
            :disabled="!options.valid_times.length"
            @clear="handleValidTimeClear"
          >
            <el-option
              v-for="item in options.valid_times"
              :key="`${item.label}-${item.cycle}`"
              :label="item.label"
              :value="item.label"
            />
          </el-select>
        </label>
        <div style="display: flex; justify-content: space-between; align-items: center; margin: 8px 0 6px;">
          <span style="color: #a1b8cb; font-size: 11px; font-weight: 600;">风场图层</span>
          <label class="switch" style="margin: 0; font-size: 11px;"><input v-model="layers.route" type="checkbox" /> 显示航线</label>
        </div>
        <div class="segmented-control" style="margin-bottom: 10px;">
          <button type="button" :class="{ active: layers.velocity }" @click="layers.velocity = true; layers.heatmap = false">风向粒子流</button>
          <button type="button" :class="{ active: layers.heatmap }" @click="layers.heatmap = true; layers.velocity = false">风速热力图</button>
        </div>
        <button class="primary" :disabled="loading || !selection.source || !selection.level || !selection.validTime || !options.valid_times.length" @click="$emit('reload')" style="margin-top: 8px;">{{ loading ? '加载中...' : '加载当前风场' }}</button>
      </section>

      <section>
        <h2 style="margin: 0 0 8px;">阈值设置</h2>
        <div class="threshold-grid">
          <label>一级风<input v-model.number="thresholds.safe" type="number" min="0" step="0.5" /></label>
          <label>二级风<input v-model.number="thresholds.notice" type="number" min="0" step="0.5" /></label>
          <label>三级风<input v-model.number="thresholds.warning" type="number" min="0" step="0.5" /></label>
          <label>四级风<input v-model.number="thresholds.danger" type="number" min="0" step="0.5" /></label>
        </div>
      </section>

      <section>
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <h2 style="margin:0">航线规划</h2>
          <button class="ghost" style="width: auto; padding: 2px 8px; font-size: 11px; margin: 0; color: #ff4757; border-color: rgba(255,71,87,0.3);" @click="$emit('clear-route')">清除航线</button>
        </div>
        <label>航线名称<input v-model="planner.name" type="text" placeholder="默认航线 A" /></label>
        <div class="grid-3" style="margin-top: 8px;">
          <label>省份选择
            <el-select
              :model-value="areaSelection.province"
              class="glass-select"
              popper-class="glass-select-popper"
              filterable
              clearable
              placeholder="请选择省份"
              @update:model-value="handleAreaChange('province', $event)"
              @clear="handleAreaChange('province', '')"
            >
              <el-option v-for="item in areaPresets.province" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </label>
          <label>片区选择
            <el-select
              :model-value="areaSelection.region"
              class="glass-select"
              popper-class="glass-select-popper"
              filterable
              clearable
              placeholder="请选择片区"
              @update:model-value="handleAreaChange('region', $event)"
              @clear="handleAreaChange('region', '')"
            >
              <el-option v-for="item in areaPresets.region" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </label>
          <label>项目选择
            <el-select
              :model-value="areaSelection.project"
              class="glass-select"
              popper-class="glass-select-popper"
              filterable
              clearable
              placeholder="请选择项目"
              @update:model-value="handleAreaChange('project', $event)"
              @clear="handleAreaChange('project', '')"
            >
              <el-option v-for="item in areaPresets.project" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </label>
        </div>
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
          <button class="primary" :disabled="loading || !planner.startText || !planner.endText" @click="$emit('plan-route')" style="margin: 0;">
            <span v-if="planner.planning" class="loading-spinner"></span>
            生成规划航线
          </button>
          <button class="ghost" :disabled="!planner.points.length" @click="$emit('save-route')" style="margin: 0;">应用航线</button>
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
            <button class="ghost saved-route-button" @click="$emit('plan-route', route)">
              <span class="saved-route-name">{{ route.name }}</span>
              <span class="saved-route-date">{{ formatRouteDate(route.created_at) }}</span>
            </button>
            <button class="icon-button" title="删除" @click="$emit('delete-route', route.route_id)">×</button>
          </div>
        </div>
      </section>
    </div>
  </aside>
</template>
