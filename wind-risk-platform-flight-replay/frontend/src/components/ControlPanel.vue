<script setup>
import WorkspaceIcon from './WorkspaceIcon.vue'
import { computed, nextTick, onMounted, ref } from 'vue'
import { exportForRoute, routeDistanceKm } from '../utils/routeRecords'
import { savedRouteFromExport } from '../utils/customRouteDrawing'
import { departureRangeBounds } from '../utils/departureRange'
import {
  deleteExportedRoute,
  getExportedRouteUrl,
  getExportedWaypointUrl,
  listExportedRoutes,
  parseWaypointText,
  renameExportedRouteFile,
  renameExportedRouteName,
} from '../api'

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
  flightSettings: { type: Object, required: true },
  planner: { type: Object, required: true },
  savedRoutes: { type: Array, required: true },
  areaSelection: { type: Object, required: true },
  areaPresets: { type: Object, required: true },
  loading: Boolean,
  replayActive: Boolean,
  nativeLevel: { type: String, default: '' },
  picking: { type: String, default: '' },
  drawingActive: Boolean,
  drawingSaving: Boolean,
  savedCurrent: Boolean,
  savingRoute: Boolean,
  saveError: String,
  planningStatus: Object,
  recommendationBaseline: Object,
  recommendationRange: {type:Object,required:true},
  planningEntry: {type:String,default:'specified'},
  detourSegment: { type: Number, default: 0 },
  maxDetourPercent: { type: Number, default: 50 },
  recommending: Boolean,
  previewingRecommendation: Boolean,
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

const emit = defineEmits(['reload', 'clear-route', 'pick-start', 'pick-end', 'plan-route', 'cancel-plan', 'save-route', 'load-routes', 'delete-route', 'focus-area', 'import-route-file', 'draw-custom-route', 'recommend-range', 'export-route', 'notice', 'recommend-detour', 'detour-segment', 'detour-limit', 'cancel-recommendation'])
const controlTabs = [
  { id: 'weather', label: '风场预测可视化' },
  { id: 'specified', label: '指定时间规划航线' },
  { id: 'recommended', label: '推荐最佳起飞时间' },
  { id: 'history', label: '历史航线回放' },
  { id: 'settings', label: '参数配置' },
]
const activeTab = ref('weather')
const tabListRef = ref()
const panelContentRef = ref()
const tabScrollPositions = new Map()
const selectTab = async (id, focus = false) => {
  tabScrollPositions.set(activeTab.value, panelContentRef.value?.scrollTop || 0)
  activeTab.value = id
  await nextTick()
  if (activeTab.value !== id) return
  if (panelContentRef.value) panelContentRef.value.scrollTop = tabScrollPositions.get(id) || 0
  if (focus) tabListRef.value?.querySelector(`#control-tab-${id}`)?.focus()
}
const navigateTabs = (event, index) => {
  const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -2, ArrowDown: 2 }
  if (!(event.key in offsets) && event.key !== 'Home' && event.key !== 'End') return
  event.preventDefault()
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? controlTabs.length - 1
    : (index + offsets[event.key] + controlTabs.length) % controlTabs.length
  selectTab(controlTabs[next].id, true)
}
const specifiedPlanning = computed(() => props.planner.planning && props.planningEntry !== 'recommended')
const rangePlanning = computed(() => props.planner.planning && props.planningEntry === 'recommended')
const rangeBoundsError = computed(() => {
  if (!props.recommendationRange.enabled) return ''
  try { departureRangeBounds(props.recommendationRange); return '' } catch (reason) { return reason.message }
})

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
const historySearch = ref('')
const exportSearch = ref('')
const routeRows = computed(() => props.savedRoutes.map(route => {
  const distance = routeDistanceKm(route.points)
  return { ...route, distanceText: Number.isFinite(distance) ? `约 ${distance.toFixed(1)} km` : '里程未知' }
}))
const filteredRoutes = computed(() => routeRows.value.filter(route => route.name.toLocaleLowerCase().includes(historySearch.value.trim().toLocaleLowerCase())))
const filteredExports = computed(() => jsonFiles.value.filter(row => (row.route_name || '').toLocaleLowerCase().includes(exportSearch.value.trim().toLocaleLowerCase())).map(row => {
  const route = savedRouteFromExport(row, routeRows.value)
  return { ...row, sourceLabel: route ? (route.source || 'wrf').toUpperCase() : '未登记', level: route?.level || '未登记', distanceText: route?.distanceText || '未登记' }
}))
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

const downloadFile = (url, fileName) => {
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

const exportRouteFile = (routeFile, format) => {
  if (format === 'waypoints') {
    if (!routeFile?.waypoint_file_name) return
    downloadFile(getExportedWaypointUrl(routeFile.waypoint_file_name), routeFile.waypoint_file_name)
    return
  }
  if (!routeFile?.file_name) return
  downloadFile(getExportedRouteUrl(routeFile.file_name), routeFile.file_name)
}

const exportSavedRoute = async (route, format) => {
  try {
    await refreshJsonDialog()
    const file = exportForRoute(route, jsonFiles.value, props.savedRoutes)
    if (!file) throw new Error('未找到该航线对应的导出文件')
    if (format === 'waypoints' && !file.waypoint_file_name) throw new Error('该航线暂无航点文件')
    exportRouteFile(file, format)
  } catch (reason) { emit('notice', reason.response?.data?.detail || reason.message) }
}

const triggerJsonImport = () => {
  jsonImportInput.value?.click()
}

const nameFromRouteFile = (fileName) => {
  return fileName.replace(/\.(json|waypoints)$/i, '').replace(/_\d{14}$/, '') || '导入航线'
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
    const isWaypointFile = /\.waypoints$/i.test(file.name)
    const isJsonFile = /\.json$/i.test(file.name)
    if (!isWaypointFile && !isJsonFile) throw new Error('请选择 .json 或 .waypoints 航线文件')
    const payload = isWaypointFile
      ? await parseWaypointText(await file.text())
      : JSON.parse(await file.text())
    const points = extractRoutePoints(payload)
    if (points.length < 2) throw new Error('航线文件至少需要包含 2 个有效航点')
    const name = payload?.mission_name || payload?.name || payload?.route_name || nameFromRouteFile(file.name)
    const missionItems = Array.isArray(payload?.mission_items) ? payload.mission_items : null
    emit('import-route-file', { name, points, missionItems, raw: payload })
    showJsonDialog.value = false
  } catch (error) {
    console.error('导入航线文件失败', error)
    emit('notice', error.response?.data?.detail || error.message || '导入航线文件失败')
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
    emit('notice', error.response?.data?.detail || error.message || '重命名 JSON 文件失败')
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
    emit('notice', error.response?.data?.detail || error.message || '重命名历史航线失败')
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
  const route = savedRouteFromExport(row, props.savedRoutes)
  if (route) {
    emit('plan-route', route)
  } else {
    emit('notice', '无法确定对应航线，请从左侧历史列表选择具体记录')
  }
}

const beginCustomDrawing = () => {
  showJsonDialog.value = false
  emit('draw-custom-route')
}

onMounted(refreshJsonDialog)
defineExpose({ refreshJsonDialog })

</script>

<template>
  <!-- JSON 文件列表弹窗 -->
  <el-dialog v-model="showJsonDialog" title="历史航线列表" width="min(1100px, calc(100vw - 24px))" class="glass-dialog" destroy-on-close>
    <div style="display: flex; justify-content: flex-end; gap: 8px; margin-bottom: 12px;">
      <button class="ghost" :disabled="drawingActive || drawingSaving" style="width: auto; padding: 4px 10px; margin: 0; font-size: 12px; display: flex; align-items: center; gap: 4px;" @click="triggerJsonImport">
        <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
        导入航线文件
      </button>
      <input type="file" ref="jsonImportInput" style="display: none;" accept=".json,.waypoints" @change="handleJsonImport" />
    </div>
    <input v-model="exportSearch" class="history-search" type="search" aria-label="搜索历史航线列表" placeholder="搜索航线名称" />
    <el-table :data="filteredExports" class="glass-table" style="width: 100%" height="400">
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
          <div v-else @click="startRenameJsonRoute(scope.row)" style="cursor: pointer; width: 100%; min-height: 24px; display: flex; flex-direction: column; justify-content: center; align-items: flex-start;">
            <span>{{ scope.row.file_name }}</span>
            <span v-if="scope.row.waypoint_file_name" class="subtle" style="font-size: 12px;">{{ scope.row.waypoint_file_name }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="sourceLabel" label="数据源" width="90" />
      <el-table-column prop="level" label="高度" min-width="115" />
      <el-table-column prop="distanceText" label="里程" width="115" />
      <el-table-column prop="time" label="时间" width="180" />
      <el-table-column label="操作" width="220" fixed="right" align="center">
        <template #default="scope">
          <div style="display: flex; justify-content: center; gap: 8px;">
            <el-popover
              placement="bottom-end"
              trigger="click"
              :width="156"
              popper-class="route-export-popover">
              <template #reference>
                <el-button type="primary" link size="small" class="glass-table-btn">
                  导出
                </el-button>
              </template>
              <div class="route-export-options">
                <button type="button" class="route-export-option" @click="exportRouteFile(scope.row, 'json')">
                  <span class="route-export-option-icon">JSON</span>
                  <span>导出 JSON</span>
                </button>
                <button
                  type="button"
                  class="route-export-option"
                  :disabled="!scope.row.waypoint_file_name"
                  @click="exportRouteFile(scope.row, 'waypoints')">
                  <span class="route-export-option-icon">WPT</span>
                  <span>导出航点文件</span>
                </button>
              </div>
            </el-popover>
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
  <div class="panel-body control-body" :inert="drawingSaving" :aria-busy="drawingSaving">
    <div ref="tabListRef" class="control-tabs" role="tablist" aria-label="规划与航线功能">
      <button v-for="(tab, index) in controlTabs" :key="tab.id" :id="`control-tab-${tab.id}`" type="button"
        role="tab" :class="{ active: activeTab === tab.id }" :aria-selected="activeTab === tab.id"
        :aria-controls="`control-tabpanel-${tab.id}`" :tabindex="activeTab === tab.id ? 0 : -1"
        @click="selectTab(tab.id)" @keydown="navigateTabs($event, index)"><WorkspaceIcon :name="tab.id" /><span>{{ tab.label }}</span></button>
    </div>
    <div ref="panelContentRef" class="panel-content">
      <div v-show="activeTab === 'weather'" id="control-tabpanel-weather" class="control-tab-panel" role="tabpanel" aria-labelledby="control-tab-weather" tabindex="0">
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
            <input v-if="nativeLevel" :value="nativeLevel" readonly aria-label="当前 V3 风场高度" />
            <el-select v-else v-model="selection.level" class="glass-select" popper-class="glass-select-popper" filterable
              clearable placeholder="请选择高度层" @clear="handleLevelClear">
              <el-option v-for="level in options.levels" :key="level" :label="level" :value="level" />
            </el-select>
          </label>
        </div>
        <label>单时刻风场选择
          <el-select v-model="selection.validTime" class="glass-select" popper-class="glass-select-popper" filterable
            clearable placeholder="请选择风场时刻" :disabled="!options.valid_times.length" @clear="handleValidTimeClear">
            <el-option v-for="item in options.valid_times" :key="`${item.label}-${item.cycle}`" :label="item.label"
              :value="item.label" />
          </el-select>
        </label>
        <p v-if="replayActive" class="subtle" style="font-size: 12px; line-height: 1.6;">底图当前跟随飞行回放时间。点击“加载风场”可查看这里选择的单时刻剖面。</p>
        <div v-if="selection.source === 'wrf' && options.lookahead" class="subtle" role="status" style="margin-top: 8px; line-height: 1.6;">
          <template v-if="!options.lookahead.available">
            {{ options.lookahead.target_valid_time_bj }} 的 WRF 产物尚未就绪，每 10 分钟自动查询，当前显示已有数据。
          </template>
          <template v-if="options.remote_sync?.running">正在同步最新 WRF 风场，完成后自动更新。</template>
          <template v-else-if="options.remote_sync?.last_error">同步暂未成功，将自动重试。</template>
        </div>
        <div style="display: flex; justify-content: space-between; align-items: center; margin: 12px 0 8px;">
          <h3 class="layer-section-title">风场图层</h3>
        </div>
        <div class="segmented-control" style="margin-bottom: 10px;">
          <button type="button" :class="{ active: layers.velocity }"
            @click="layers.velocity = true; layers.heatmap = false">风向粒子流</button>
          <button type="button" :class="{ active: layers.heatmap }"
            @click="layers.heatmap = true; layers.velocity = false">风速热力图</button>
        </div>
        <div v-if="layers.heatmap" style="margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; background: rgba(0, 0, 0, 0.2); border-radius: 4px; border: 1px solid rgba(0, 243, 255, 0.1);">
          <span style="color: #00f3ff; font-size: 12px;">水平风切变区域高亮</span>
          <div class="cyber-switch" :class="{ 'is-active': layers.mutation }" @click="layers.mutation = !layers.mutation">
            <div class="switch-handle"></div>
          </div>
        </div>
        <button class="secondary"
          :disabled="loading || !selection.source || !selection.level || !selection.validTime || !options.valid_times.length"
          @click="$emit('reload')" style="margin-top: 8px;">{{ loading ? '加载中...' : '加载风场' }}</button>
      </section>

      <!-- 2. 区域选择 -->
      <section class="region-location">

        <h2 style="margin: 0 0 12px;">区域定位</h2>
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

      </div>
      <div v-show="activeTab === 'specified'" id="control-tabpanel-specified" class="control-tab-panel" role="tabpanel" aria-labelledby="control-tab-specified" tabindex="0">
      <section class="route-planning-section" :inert="drawingActive" :aria-disabled="drawingActive">
        <div class="section-heading"><h2>指定时间规划航线</h2><button class="text-button danger-text" @click="$emit('clear-route')">清除航线</button></div>
        <label>航线名称<input v-model="planner.name" type="text" maxlength="120" placeholder="请输入航线名称" /></label>
        <label>起点<div class="input-group"><input v-model="planner.startText" type="text" placeholder="经度, 纬度" />
          <button class="ghost" :class="{ 'active-pick': picking === 'start' }" @click="$emit('pick-start')">{{ picking === 'start' ? '选点中' : '选点' }}</button></div></label>
        <label>终点<div class="input-group"><input v-model="planner.endText" type="text" placeholder="经度, 纬度" />
          <button class="ghost" :class="{ 'active-pick': picking === 'end' }" @click="$emit('pick-end')">{{ picking === 'end' ? '选点中' : '选点' }}</button></div></label>
        <label>起飞时间（北京时间）<select v-model="planner.departureMode" aria-label="规划起飞方式">
          <option value="now">现在起飞</option><option value="scheduled">选择未来起飞时间</option>
        </select></label>
        <input v-if="planner.departureMode === 'scheduled'" v-model="planner.departureAt" aria-label="规划起飞时间（北京时间）" type="datetime-local" step="60" />
        <label>规划策略<el-select v-model="planner.strategy" class="glass-select" popper-class="glass-select-popper">
          <el-option v-for="item in planningStrategies" :key="item.value" :label="item.label" :value="item.value" />
        </el-select></label>
        <button v-if="!specifiedPlanning" class="primary" :disabled="loading || savingRoute || !planner.startText || !planner.endText" @click="$emit('plan-route', {forceReplan:Boolean(planningStatus?.cache_hit)})">{{ planningStatus?.cache_hit ? '重新优化航线' : '规划航线' }}</button>
        <button v-else class="secondary" @click="$emit('cancel-plan')">停止当前规划</button>
      </section>
      </div>

      <div v-show="activeTab === 'recommended'" id="control-tabpanel-recommended" class="control-tab-panel" role="tabpanel" aria-labelledby="control-tab-recommended" tabindex="0">
      <section class="route-recommendation-section">
        <h2>推荐最佳起飞时间</h2>
        <label class="recommendation-range-toggle"><input v-model="recommendationRange.enabled" type="checkbox" :disabled="drawingActive" aria-label="启用起飞时间段限制" />起飞时间段限制</label>
        <template v-if="recommendationRange.enabled">
          <label>最早起飞（北京时间）<input v-model="recommendationRange.departureStartAt" :disabled="drawingActive" aria-label="起飞时间段开始（北京时间）" type="datetime-local" step="60" /></label>
          <label>最晚起飞（北京时间）<input v-model="recommendationRange.departureEndAt" :disabled="drawingActive" aria-label="起飞时间段结束（北京时间）" type="datetime-local" step="60" /></label>
          <p v-if="rangeBoundsError" class="save-error" role="alert">{{ rangeBoundsError }}</p>
        </template>
        <button class="primary" :disabled="drawingSaving || drawingActive || Boolean(rangeBoundsError)" @click="$emit('draw-custom-route')">自定义绘制航线</button>
        <template v-if="recommendationRange.enabled && recommendationRange.requiredWaypoints.length && !drawingActive">
          <p>{{ recommendationRange.name }} · {{ recommendationRange.requiredWaypoints.length }} 个必经航点</p>
          <button v-if="!rangePlanning" class="primary" :disabled="Boolean(rangeBoundsError) || savingRoute" @click="$emit('recommend-range')">重新推荐起飞时间与航线</button>
          <button v-else class="secondary" @click="$emit('cancel-plan')">停止推荐，保留已确认航线</button>
        </template>
        <template v-if="recommendationBaseline && !drawingActive && !recommendationRange.enabled">
          <p>{{ recommendationBaseline.name }} · {{ recommendationBaseline.points.length }} 个固定航点</p>
          <label>绕行航段（相邻航点）
            <select :value="detourSegment" aria-label="绕行航段" @change="$emit('detour-segment', Number($event.target.value))">
              <option v-for="(_, index) in recommendationBaseline.points.slice(1)" :key="index" :value="index">航点 {{ index + 1 }} → {{ index + 2 }}</option>
            </select>
          </label>
          <label>允许增加里程上限（%）<input :value="maxDetourPercent" aria-label="允许绕行增加里程百分比" type="number" min="0" step="10" @change="$emit('detour-limit', Number($event.target.value))" /></label>
          <button v-if="!recommending" class="primary" :disabled="!Number.isFinite(maxDetourPercent) || maxDetourPercent < 0" @click="$emit('recommend-detour')">推荐绕行</button>
          <button v-else class="secondary" @click="$emit('cancel-recommendation')">停止推荐，保留已确认方案</button>
        </template>
      </section>
      </div>

      <div v-show="activeTab === 'history'" id="control-tabpanel-history" class="control-tab-panel" role="tabpanel" aria-labelledby="control-tab-history" tabindex="0">
      <section class="route-history-section">
        <div class="section-heading"><h2>历史航线</h2><button class="text-button" @click="openJsonDialog">更多...</button></div>
        <input v-model="historySearch" type="search" class="history-search" aria-label="搜索历史航线" placeholder="搜索航线名称" />
        <div class="route-list">
          <p v-if="!savedRoutes.length" class="empty-inline">暂无保存的航线</p>
          <p v-else-if="!filteredRoutes.length" class="empty-inline">未找到匹配航线</p>
          <article v-for="route in filteredRoutes" :key="route.route_id" class="saved-route" :data-route-id="route.route_id">
            <button class="saved-route-button ghost" @click="$emit('plan-route', route)">
              <span class="saved-route-name" :title="route.name">{{ route.name }}</span>
              <span class="saved-route-meta">{{ (route.source || 'wrf').toUpperCase() }} · {{ route.level }} · {{ route.distanceText }}</span>
              <span class="saved-route-date">{{ formatRouteDate(route.created_at) }}</span>
            </button>
            <div class="saved-route-actions">
              <el-popover trigger="click" :width="172" popper-class="route-export-popover">
                <template #reference><button class="text-button" title="导出航线">导出</button></template>
                <div class="route-export-options"><button class="route-export-option" @click="exportSavedRoute(route, 'json')">导出 JSON</button>
                  <button class="route-export-option" @click="exportSavedRoute(route, 'waypoints')">导出航点文件</button></div>
              </el-popover>
              <button class="text-button" title="回放航线" @click="$emit('plan-route', route)">回放</button>
              <button class="text-button danger-text" title="删除航线" @click="$emit('delete-route', route.route_id)">删除</button>
            </div>
          </article>
        </div>
      </section>
      </div>

      <div v-show="activeTab === 'settings'" id="control-tabpanel-settings" class="control-tab-panel" role="tabpanel" aria-labelledby="control-tab-settings" tabindex="0">
      <section class="flight-settings-section">
        <h2>飞行参数</h2>
        <label v-if="nativeLevel || planner.algorithm === 'wrf_v3'">固定空速 (m/s)<input value="10" type="number" disabled /></label>
        <label v-else>平均对地速度 (m/s)<input v-model.number="flightSettings.averageGroundSpeedMps" type="number" min="0" step="any" /></label>
      </section>

      <section class="advanced-settings">
        <h2>规划与风险参数</h2>
        <div>
          <div :inert="drawingActive">
            <label>规划算法<el-select v-model="planner.algorithm" class="glass-select" popper-class="glass-select-popper">
              <el-option v-for="item in plannerTypes" :key="item.value" :label="item.label" :value="item.value" />
            </el-select></label>
            <label>机型<el-select v-model="planner.aircraftModel" class="glass-select" popper-class="glass-select-popper">
              <el-option v-for="item in aircraftModels" :key="item.value" :label="item.label" :value="item.value" />
            </el-select></label>
          </div>
          <h3>风险阈值</h3>
          <div class="threshold-grid">
            <label>一级风<input v-model.number="thresholds.safe" type="number" min="0" step="0.5" /></label>
            <label>二级风<input v-model.number="thresholds.notice" type="number" min="0" step="0.5" /></label>
            <label>三级风<input v-model.number="thresholds.warning" type="number" min="0" step="0.5" /></label>
            <label>四级风<input v-model.number="thresholds.danger" type="number" min="0" step="0.5" /></label>
            <label class="threshold-wide">最大水平风切变 (m/s)<input v-model.number="thresholds.maxHorizontalWindShear" type="number" min="0.1" step="0.1" /></label>
          </div>
          <div v-if="planner.algorithm === 'wrf_v3'" class="v3-settings">
            <label>固定海拔高度 MSL（米）<input v-model.number="planner.altitudeMslM" type="number" min="1" max="20000" step="10" /></label>
            <label><input v-model="planner.constrainHeadings" type="checkbox" />约束起终点航向</label>
            <div v-if="planner.constrainHeadings" class="grid-2">
              <label>起点航向（°）<input v-model.number="planner.startHeadingDeg" type="number" min="0" max="360" /></label>
              <label>终点航向（°）<input v-model.number="planner.endHeadingDeg" type="number" min="0" max="360" /></label>
            </div>
          </div>
        </div>
      </section>
      </div>
    </div>
    <footer v-if="!drawingActive && (activeTab === 'specified' || planner.planning)" class="planning-actions">
      <div v-if="planningStatus?.label" class="planning-progress" role="status">
        <strong>{{ planningStatus.label }}</strong><span v-if="Number.isFinite(planningStatus.elapsed)">已用时 {{ Math.floor(planningStatus.elapsed) }} 秒</span>
      </div>
      <p v-if="Number.isFinite(planningStatus?.queue_wait_seconds)" class="planning-message">排队 {{ planningStatus.queue_wait_seconds.toFixed(1) }}s · 快照准备 {{ (planningStatus.preparation_seconds || 0).toFixed(1) }}s</p>
      <p v-if="planner.planningMessage && !planner.planning" class="planning-message">{{ planner.planningMessage }}</p>
      <button v-if="planner.planning" class="secondary" @click="$emit('cancel-plan')">{{ planner.points.length ? '停止优化，保留当前航线' : '取消规划' }}</button>
      <div v-if="activeTab === 'specified'" class="grid-2">
        <button class="ghost" :disabled="!planner.points.length || savingRoute || savedCurrent || previewingRecommendation" @click="$emit('save-route')">{{ previewingRecommendation ? '采用后可保存' : savingRoute ? '保存中…' : savedCurrent ? '已保存' : '保存航线' }}</button>
        <el-popover trigger="click" :width="180" popper-class="route-export-popover" :disabled="!savedCurrent || savingRoute || previewingRecommendation">
          <template #reference><span :title="previewingRecommendation ? '采用并保存后可导出' : savedCurrent ? '导出当前已保存版本' : '请先保存当前航线'" class="export-trigger"><button class="ghost" :disabled="!savedCurrent || savingRoute || previewingRecommendation">导出</button></span></template>
          <div class="route-export-options"><button class="route-export-option" @click="$emit('export-route', 'json')">导出 JSON</button>
            <button class="route-export-option" @click="$emit('export-route', 'waypoints')">导出航点文件</button></div>
        </el-popover>
      </div>
      <p v-if="activeTab === 'specified' && saveError" class="save-error" role="alert">{{ saveError }}</p>
    </footer>
    <div v-if="drawingActive" class="drawing-panel-note">正在绘制航线，请使用地图下方工具栏完成绘制。</div>
  </div>
</template>

<style scoped>
.recommendation-range-toggle { display:flex; align-items:center; gap:8px; min-height:36px; color:#fff; }
.recommendation-range-toggle input { width:18px; height:18px; margin:0; accent-color:#35bdde; }
</style>
