<script setup>
import { ref } from 'vue'
import { listExportedRoutes, getExportedRouteUrl, deleteExportedRoute, renameExportedRouteFile, renameExportedRouteName } from '../api'

const dataSources = [
  { value: 'gfs', label: 'GFS 原始数据' },
  { value: 'wrf', label: 'WRF 降尺度' },
]

const plannerTypes = [
  { value: 'astar', label: 'A*' },
  { value: 'lpa_star', label: 'LPA*' },
  { value: 'wa_lpa_star', label: 'WA-LPA*' },
]

const aircraftModels = [
  { value: 'fixed_wing', label: '固定翼无人机' },
]

const planningStrategies = [
  { value: 'distance_priority', label: '路程优先' },
  { value: 'balanced', label: '均衡避险' },
  { value: 'wind_avoidance', label: '避风优先' },
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

const vFocus = {
  mounted: (el) => {
    // el might be the input itself or a wrapper
    if (el.tagName === 'INPUT') {
      el.focus()
    } else {
      const input = el.querySelector('input')
      if (input) input.focus()
    }
  }
}

const emit = defineEmits(['reload', 'clear-route', 'pick-start', 'pick-end', 'plan-route', 'save-route', 'load-routes', 'delete-route', 'focus-area', 'import-json-route'])

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
  if (!value) return '未记录日期'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未记录日期'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).format(date)
}

const showJsonDialog = ref(false)
const jsonFiles = ref([])
const jsonImportInput = ref()
const editingJsonFile = ref('')
const editingJsonFileName = ref('')
const editingJsonRouteFile = ref('')
const editingJsonRouteName = ref('')

const openJsonDialog = async () => {
  showJsonDialog.value = true
  await refreshJsonDialog()
}

const refreshJsonDialog = async () => {
  try {
    jsonFiles.value = await listExportedRoutes()
  } catch (error) {
    console.error('获取JSON文件列表失败', error)
  }
}

const viewJsonFile = (fileName) => {
  window.open(getExportedRouteUrl(fileName), '_blank')
}

const triggerExportByName = async (routeName) => {
  try {
    const files = await listExportedRoutes()
    const targetFile = files.find(f => f.route_name === routeName)
    if (targetFile) {
      viewJsonFile(targetFile.file_name)
    } else {
      window.alert('未找到该航线对应的 JSON 文件')
    }
  } catch (error) {
    console.error('查找导出文件失败', error)
  }
}

const triggerJsonImport = () => {
  jsonImportInput.value?.click()
}

const nameFromJsonFile = (fileName) => {
  return fileName.replace(/\.json$/i, '').replace(/_\d{14}$/, '') || '导入航线'
}

const extractRoutePoints = (payload) => {
  const candidates = Array.isArray(payload)
    ? payload
    : payload?.points || payload?.waypoints || payload?.route || payload?.coordinates || []
  if (!Array.isArray(candidates)) return []
  return candidates.map((point) => {
    if (Array.isArray(point)) {
      const result = [Number(point[0]), Number(point[1])]
      point.slice(2).map(Number).forEach((value) => {
        if (Number.isFinite(value)) result.push(value)
      })
      return result
    }
    if (point && typeof point === 'object') {
      const lon = Number(point.lon ?? point.longitude ?? point.lng)
      const lat = Number(point.lat ?? point.latitude)
      const altitudeAmsl = Number(point.altitude_amsl_m ?? point.altitude_m ?? point.ele)
      const altitudeAgl = Number(point.altitude_agl_m ?? point.agl_m)
      const terrain = Number(point.terrain_height_m ?? point.terrain_alt_m ?? point.hgt_surface_m)
      const result = [lon, lat]
      if (Number.isFinite(altitudeAmsl)) result.push(altitudeAmsl)
      else if (Number.isFinite(altitudeAgl) && Number.isFinite(terrain)) result.push(altitudeAgl + terrain)
      if (Number.isFinite(altitudeAgl)) result.push(altitudeAgl)
      if (Number.isFinite(terrain)) result.push(terrain)
      return result
    }
    return [Number.NaN, Number.NaN]
  }).filter(([lon, lat]) => Number.isFinite(lon) && Number.isFinite(lat))
}

const handleJsonImport = async (event) => {
  const file = event.target.files?.[0]
  if (!file) return
  try {
    const payload = JSON.parse(await file.text())
    const points = extractRoutePoints(payload)
    if (points.length < 2) throw new Error('JSON 至少需要包含 2 个有效航点')
    const name = payload?.mission_name || payload?.name || payload?.route_name || nameFromJsonFile(file.name)
    emit('import-json-route', { name, points, raw: payload })
    showJsonDialog.value = false
  } catch (error) {
    console.error('导入 JSON 航线失败', error)
    window.alert(error.message || '导入 JSON 航线失败')
  } finally {
    event.target.value = ''
  }
}

const startRenameJsonRoute = (row) => {
  editingJsonFile.value = row.file_name
  editingJsonFileName.value = row.file_name || ''
}

const confirmRenameJsonFile = async (row) => {
  if (editingJsonFile.value !== row.file_name) return
  const fileName = editingJsonFileName.value.trim()
  editingJsonFile.value = ''
  editingJsonFileName.value = ''
  if (!fileName || fileName === row.file_name) {
    return
  }
  try {
    const updated = await renameExportedRouteFile(row.file_name, fileName)
    jsonFiles.value = jsonFiles.value.map((item) => item.file_name === row.file_name ? updated : item)
  } catch (error) {
    console.error('重命名 JSON 文件失败', error)
    window.alert(error.response?.data?.detail || error.message || '重命名 JSON 文件失败')
  }
}

const startRenameJsonRouteName = (row) => {
  editingJsonRouteFile.value = row.file_name
  editingJsonRouteName.value = row.route_name || ''
}

const confirmRenameJsonRouteName = async (row) => {
  if (editingJsonRouteFile.value !== row.file_name) return
  const routeName = editingJsonRouteName.value.trim()
  editingJsonRouteFile.value = ''
  editingJsonRouteName.value = ''
  if (!routeName || routeName === row.route_name) {
    return
  }
  try {
    const updated = await renameExportedRouteName(row.file_name, routeName)
    jsonFiles.value = jsonFiles.value.map((item) => item.file_name === row.file_name ? updated : item)
    emit('load-routes')
  } catch (error) {
    console.error('重命名历史航线失败', error)
    window.alert(error.response?.data?.detail || error.message || '重命名历史航线失败')
  }
}

const removeJsonFile = async (fileName) => {
  try {
    await deleteExportedRoute(fileName)
    // 刷新列表
    jsonFiles.value = await listExportedRoutes()
    emit('load-routes')
  } catch (error) {
    console.error('删除JSON文件失败', error)
  } 
}

const playRouteFromDialog = (row) => {
  showJsonDialog.value = false
  // find route in savedRoutes by name or file
  const route = props.savedRoutes.find(r => r.name === row.route_name)
  if (route) {
    emit('plan-route', route)
  } else {
    // If not found in DB but exists in JSON list, we might want to alert or handle it.
    // Assuming DB has it if it's in the json list for now.
    window.alert('该航线在历史记录中未找到，可能已被删除')
  }
}

defineExpose({ refreshJsonDialog })

</script>

<template>
  <!-- JSON 文件列表弹窗 -->
  <el-dialog v-model="showJsonDialog" title="历史航线列表" width="65%" class="glass-dialog" destroy-on-close>
    <div style="display: flex; justify-content: flex-end; margin-bottom: 12px;">
      <button class="ghost" style="width: auto; padding: 4px 10px; margin: 0; font-size: 11px; display: flex; align-items: center; gap: 4px;" @click="triggerJsonImport">
        <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
        导入 JSON
      </button>
      <input type="file" ref="jsonImportInput" style="display: none;" accept=".json" @change="handleJsonImport" />
    </div>
    <el-table :data="jsonFiles" class="glass-table" style="width: 100%" height="400">
      <el-table-column label="航线名称" min-width="120">
        <template #default="scope">
          <div v-if="editingJsonRouteFile === scope.row.file_name" style="display: flex; gap: 4px; align-items: center;">
            <input class="glass-input-inline" v-model="editingJsonRouteName" v-focus @blur="confirmRenameJsonRouteName(scope.row)" @keyup.enter="confirmRenameJsonRouteName(scope.row)" />
          </div>
          <div v-else @click="startRenameJsonRouteName(scope.row)" style="cursor: pointer; width: 100%; min-height: 24px; display: flex; align-items: center;">
            {{ scope.row.route_name }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="文件名称" min-width="150">
        <template #default="scope">
          <div v-if="editingJsonFile === scope.row.file_name" style="display: flex; gap: 4px; align-items: center;">
            <input class="glass-input-inline" v-model="editingJsonFileName" v-focus @blur="confirmRenameJsonFile(scope.row)" @keyup.enter="confirmRenameJsonFile(scope.row)" />
          </div>
          <div v-else @click="startRenameJsonRoute(scope.row)" style="cursor: pointer; width: 100%; min-height: 24px; display: flex; align-items: center;">
            {{ scope.row.file_name }}
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="time" label="时间" width="180" />
      <el-table-column label="操作" width="220" fixed="right" align="center">
        <template #default="scope">
          <div style="display: flex; justify-content: center; gap: 8px;">
            <el-button type="primary" link size="small" class="glass-table-btn"
              @click="viewJsonFile(scope.row.file_name)">
              导出
            </el-button>
            <el-button type="success" link size="small" class="glass-table-btn"
              @click="playRouteFromDialog(scope.row)">
              回放
            </el-button>
            <el-button type="danger" link size="small"
              @click="removeJsonFile(scope.row.file_name)">
              删除
            </el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>
  <aside class="panel control-panel">
    <div class="panel-content">
      <!-- 1. 数据选择 -->
      <section style="margin-top: 0;">
        <h2 style="margin: 0 0 12px;">数据选择</h2>
        <div class="grid-2">
          <label>数据源
            <el-select v-model="selection.source" class="glass-select" popper-class="glass-select-popper" filterable
              clearable placeholder="请选择数据源" @clear="handleSourceClear">
              <el-option v-for="item in dataSources" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </label>
          <label>高度层
            <el-select v-model="selection.level" class="glass-select" popper-class="glass-select-popper" filterable
              clearable placeholder="请选择高度层" @clear="handleLevelClear">
              <el-option v-for="level in options.levels" :key="level" :label="level" :value="level" />
            </el-select>
          </label>
        </div>
        <label>时间选择
          <el-select v-model="selection.validTime" class="glass-select" popper-class="glass-select-popper" filterable
            clearable placeholder="请选择风场时刻" :disabled="!options.valid_times.length" @clear="handleValidTimeClear">
            <el-option v-for="item in options.valid_times" :key="`${item.label}-${item.cycle}`" :label="item.label"
              :value="item.label" />
          </el-select>
        </label>
        <div style="display: flex; justify-content: space-between; align-items: center; margin: 12px 0 8px;">
          <span style="color: #a1b8cb; font-size: 11px; font-weight: 600;">风场图层</span>
        </div>
        <div class="segmented-control" style="margin-bottom: 10px;">
          <button type="button" :class="{ active: layers.velocity }"
            @click="layers.velocity = true; layers.heatmap = false">风向粒子流</button>
          <button type="button" :class="{ active: layers.heatmap }"
            @click="layers.heatmap = true; layers.velocity = false">风速热力图</button>
        </div>
        <div v-if="layers.heatmap" style="margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; background: rgba(0, 0, 0, 0.2); border-radius: 4px; border: 1px solid rgba(0, 243, 255, 0.1);">
          <span style="color: #00f3ff; font-size: 12px;">突变高危区高亮</span>
          <div class="cyber-switch" :class="{ 'is-active': layers.mutation }" @click="layers.mutation = !layers.mutation">
            <div class="switch-handle"></div>
          </div>
        </div>
        <button class="primary"
          :disabled="loading || !selection.source || !selection.level || !selection.validTime || !options.valid_times.length"
          @click="$emit('reload')" style="margin-top: 8px;">{{ loading ? '加载中...' : '加载风场' }}</button>
      </section>

      <!-- 2. 区域选择 -->
      <section>

        <h2 style="margin: 0 0 12px;">区域选择</h2>
        <div class="grid-3">
          <label>片区选择
            <el-select :model-value="areaSelection.region" class="glass-select" popper-class="glass-select-popper"
              filterable clearable placeholder="请选择片区" @update:model-value="handleAreaChange('region', $event)"
              @clear="handleAreaChange('region', '')">
              <el-option v-for="item in areaPresets.region" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </label>
          <label>省份选择
            <el-select :model-value="areaSelection.province" class="glass-select" popper-class="glass-select-popper"
              filterable clearable placeholder="请选择省份" @update:model-value="handleAreaChange('province', $event)"
              @clear="handleAreaChange('province', '')">
              <el-option v-for="item in areaPresets.province" :key="item.value" :label="item.label"
                :value="item.value" />
            </el-select>
          </label>
          <label>项目选择
            <el-select :model-value="areaSelection.project" class="glass-select" popper-class="glass-select-popper"
              filterable clearable placeholder="请选择项目" @update:model-value="handleAreaChange('project', $event)"
              @clear="handleAreaChange('project', '')">
              <el-option v-for="item in areaPresets.project" :key="item.value" :label="item.label"
                :value="item.value" />
            </el-select>
          </label>
        </div>
      </section>

      <!-- 3. 航线规划 -->
      <section>
        <h2 style="width: 100%; position: relative;">
          航线规划
          <button class="ghost"
            style="position: absolute; right: 0; bottom: 6px; width: auto; padding: 2px 8px; font-size: 11px; margin: 0; color: #ff4757; border-color: rgba(255,71,87,0.3);"
            @click="$emit('clear-route')">清除航线</button>
        </h2>
        <label>机型选择
            <el-select v-model="planner.aircraftModel" class="glass-select" popper-class="glass-select-popper"
            placeholder="请选择机型">
              <el-option v-for="item in aircraftModels" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
        </label>
        <div class="grid-2" style="margin-top: 8px;">
          <label>算法选择
            <el-select v-model="planner.algorithm" class="glass-select" popper-class="glass-select-popper"
              placeholder="请选择规划算法">
              <el-option v-for="item in plannerTypes" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </label>
          <label>策略选择
            <el-select v-model="planner.strategy" class="glass-select" popper-class="glass-select-popper"
              placeholder="请选择规划策略">
              <el-option v-for="item in planningStrategies" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </label>
        </div>
        <label>航线名称
          <div class="input-group">
            <input ref="routeNameInput" v-model="planner.name" type="text" placeholder="默认航线 A" />
            <button class="ghost" style="width: auto; padding: 4px 12px; margin-left: 4px;" title="编辑" @click="$refs.routeNameInput?.focus()">修改</button>
            <button class="primary" style="width: auto; padding: 4px 12px; margin: 0 0 0 4px;" title="确认">确认</button>
          </div>
        </label>

        <div style="display: grid; gap: 8px; margin-top: 8px;">
          <label>起点
            <div class="input-group">
              <input v-model="planner.startText" type="text" placeholder="经度, 纬度" />
              <button class="ghost" :class="{ 'active-pick': picking === 'start' }" @click="$emit('pick-start')"
                style="width: 60px;">{{ picking === 'start' ? '选点中' : '选点' }}</button>
            </div>
          </label>
          <label>终点
            <div class="input-group">
              <input v-model="planner.endText" type="text" placeholder="经度, 纬度" />
              <button class="ghost" :class="{ 'active-pick': picking === 'end' }" @click="$emit('pick-end')"
                style="width: 60px;">{{ picking === 'end' ? '选点中' : '选点' }}</button>
            </div>
          </label>
        </div>
        <div class="grid-2" style="margin-top: 10px;">
          <button class="primary" :disabled="loading || !planner.startText || !planner.endText"
            @click="$emit('plan-route')" style="margin: 0;">
            <span v-if="planner.planning" class="loading-spinner"></span>
            生成规划航线
          </button>
          <button class="ghost" :disabled="!planner.points.length" @click="$emit('save-route')"
            style="margin: 0;">下载保存</button>
        </div>
      </section>

      <!-- 4. 历史航线 -->
      <section>
        <h2 style="width: 100%; position: relative;">
          历史航线
          <span style="position: absolute; right: 0; bottom: 6px; display: flex; gap: 6px;">
            <button class="ghost"
              style="width: auto; padding: 2px 8px; font-size: 11px; margin: 0;"
              @click="openJsonDialog">更多...</button>
          </span>
        </h2>
        <div class="route-list" style="max-height: 120px; overflow-y: auto;">
          <div v-if="!savedRoutes.length" class="subtle" style="text-align: center; margin-top: 10px;">暂无保存的航线</div>
          <div v-for="route in savedRoutes" :key="route.route_id" class="saved-route">
            <button class="ghost saved-route-button" @click="$emit('plan-route', route)">
              <span class="saved-route-name">{{ route.name }}</span>
              <span class="saved-route-date">{{ formatRouteDate(route.created_at) }}</span>
            </button>
            <div style="display: flex; gap: 4px; padding-left: 4px;">
              <button class="icon-button" title="导出" @click="triggerExportByName(route.name)">
                <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
              </button>
              <button class="icon-button" title="回放" @click="$emit('plan-route', route)">
                <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
              </button>
              <button class="icon-button" title="删除" @click="$emit('delete-route', route.route_id)">
                <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
              </button>
            </div>
          </div>
        </div>
      </section>

      <!-- 5. 阈值设置 -->
      <section style="margin-bottom: 0;">
        <h2 style="margin: 0 0 12px;">阈值设置</h2>
        <div class="threshold-grid">
          <label>一级风<input v-model.number="thresholds.safe" type="number" min="0" step="0.5" /></label>
          <label>二级风<input v-model.number="thresholds.notice" type="number" min="0" step="0.5" /></label>
          <label>三级风<input v-model.number="thresholds.warning" type="number" min="0" step="0.5" /></label>
          <label>四级风<input v-model.number="thresholds.danger" type="number" min="0" step="0.5" /></label>
          <label>级差突变<input v-model.number="thresholds.mutationLevelDiff" type="number" min="1" step="1" title="相邻网格风级差大于等于此值时高亮" /></label>
          <label>夹角突变<input v-model.number="thresholds.mutationAngle" type="number" min="0" max="180" step="5" title="相邻网格风向夹角大于此值时高亮" /></label>
        </div>
      </section>
    </div>

    <!-- JSON 文件列表弹窗 -->

  </aside>
</template>
