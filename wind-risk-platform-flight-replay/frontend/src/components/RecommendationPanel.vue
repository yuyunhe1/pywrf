<script setup>
import { formatDepartureTime } from '../utils/departureWindows'
import { RECOMMENDATION_COLORS, workflowLabel } from '../utils/recommendations'
const props=defineProps({ workflow: Object, previewId: String, adopting: Boolean, busy: Boolean, selectedId:String, pendingId:String })
const emit=defineEmits(['preview', 'adopt', 'close-preview', 'cancel','select'])
const activateCard=(card,event)=>{
  if(props.workflow?.mode!=='departure_range') return
  if(event.type==='keydown') {
    if(event.target!==event.currentTarget || !['Enter',' '].includes(event.key)) return
    event.preventDefault()
  }
  emit('select',card)
}
const minutes = value => Number.isFinite(value) ? `${value.toFixed(1)} 分钟` : '—'
const earliestContinuous = card => card.windows.departure_windows.some(row => Date.parse(row.start) <= Date.parse(card.first_departure_at)
  && Date.parse(card.first_departure_at) <= Date.parse(row.end) && Date.parse(row.end) > Date.parse(row.start))
</script>
<template>
  <section v-if="workflow" class="recommendation-results" aria-live="polite">
    <div class="section-heading"><h2>{{ workflow.mode === 'departure_range' ? '时间段内的低风航线推荐' : workflow.mode === 'fixed_route' ? '局部绕行推荐' : '未来起飞推荐' }}</h2>
      <button v-if="busy" class="text-button" @click="$emit('cancel')">停止</button></div>
    <p v-if="workflow.mode === 'specified_time'" class="requested-departure">原指定 {{ formatDepartureTime(workflow.requested_departure_at) }} ·
      {{ workflow.requested_status === 'allowed' ? '已确认可飞' : workflow.requested_status === 'blocked' ? '已触发硬约束' : '尚未确认' }}
      <span v-if="workflow.requested_attempt_status === 'data_insufficient'">（数据不足）</span>
      <span v-if="workflow.requested_attempt_status === 'resource_limit'">（资源受限）</span></p>
    <p role="status">{{ workflowLabel(workflow.stage) || workflow.message }}</p>
    <template v-if="workflow.mode === 'departure_range'">
      <p>{{ formatDepartureTime(workflow.departure_start_at) }} — {{ formatDepartureTime(workflow.departure_end_at) }}</p>
      <p v-if="Number.isFinite(workflow.min_verified_peak_mps)">最低已确认峰值 {{ workflow.min_verified_peak_mps.toFixed(2) }} m/s · 择短上限 {{ workflow.peak_wind_limit_mps.toFixed(2) }} m/s</p>
      <p v-if="workflow.unsearched_departure_minutes">{{ workflow.unsearched_departure_minutes }} 个分钟起飞时刻尚未搜索</p>
    </template>
    <div v-for="(card,index) in workflow.recommendations || []" :key="card.id" class="recommendation-card" :data-recommendation-id="card.id"
      :class="{ 'selectable-card':workflow.mode === 'departure_range', 'selected-card':workflow.mode === 'departure_range' && selectedId === card.id }"
      :role="workflow.mode === 'departure_range' ? 'button' : undefined" :tabindex="workflow.mode === 'departure_range' ? 0 : undefined"
      :aria-pressed="workflow.mode === 'departure_range' ? selectedId === card.id : undefined" :aria-busy="pendingId === card.id || undefined"
      :aria-label="workflow.mode === 'departure_range' ? `切换到方案${index+1}` : undefined"
      @click="activateCard(card,$event)" @keydown="activateCard(card,$event)"
      :style="{ '--recommendation-color': RECOMMENDATION_COLORS[index] }">
      <strong>方案 {{ index+1 }}{{ index === 0 ? workflow.mode === 'departure_range' ? ' · 默认推荐' : ' · 当前最早已确认' : '' }} · {{ formatDepartureTime(card.first_departure_at) }}</strong>
      <span v-if="workflow.mode === 'departure_range' && selectedId === card.id" class="selected-badge">当前显示</span>
      <span v-if="pendingId === card.id" class="switching-badge" role="status">切换中…</span>
      <p>{{ card.plan.distance_km.toFixed(2) }} km · {{ card.plan.level }}</p>
      <p v-if="workflow.mode === 'departure_range'">全程峰值 {{ card.max_contact_wind_speed_mps.toFixed(2) }} m/s · {{ busy ? '比较中' : '当前已确认结果' }}</p>
      <p>{{ earliestContinuous(card) ? '推荐起飞时刻位于已确认窗口内' : '推荐起飞时刻已单点验收' }}</p>
      <ul class="recommended-windows" v-if="card.windows.departure_windows.length">
        <li v-for="item in card.windows.departure_windows.slice(0,4)" :key="item.start+item.end">
          {{ formatDepartureTime(item.start) }}{{ item.start === item.end ? ' · 指定时刻起飞' : ' — '+formatDepartureTime(item.end) }}</li>
      </ul>
      <p v-else>仅所列起飞时刻已验证，连续窗口待确认</p>
      <div v-if="card.metrics" class="recommendation-benefits">
        <p v-if="!card.metrics.baseline_has_windows">原路径暂无确认窗口</p>
        <p v-else>最早起飞提前 {{ minutes(card.metrics.earlier_minutes) }}</p>
        <p>新增 {{ minutes(card.metrics.gained_minutes) }} · 减少 {{ minutes(card.metrics.lost_minutes) }} · 净变化 {{ minutes(card.metrics.net_minutes) }}</p>
        <p>增加 {{ card.metrics.extra_distance_km.toFixed(2) }} km · 航时增加 {{ minutes(card.metrics.extra_flight_seconds/60) }}</p>
      </div>
      <div v-if="workflow.mode !== 'departure_range'" class="grid-2"><button class="ghost" :disabled="adopting" @click="$emit('preview',card)">{{ previewId === card.id ? '正在预览' : '预览' }}</button>
        <button class="primary" :disabled="adopting" @click="$emit('adopt',card)">{{ adopting ? '复核中…' : '采用方案' }}</button></div>
    </div>
    <button v-if="previewId" class="secondary" @click="$emit('close-preview')">关闭方案预览</button>
    <details v-if="workflow.attempts?.length" class="assessment-detail"><summary>起飞时刻搜索状态（{{ workflow.attempts.length }}）</summary>
      <p v-for="(attempt,index) in workflow.attempts" :key="`${attempt.departure_at}:${attempt.phase}:${index}`">{{ formatDepartureTime(attempt.departure_at) }} ·
        <span v-if="attempt.phase">{{ ({seed:'避风搜索',tighten:'降低峰值',shorten:'压缩里程'})[attempt.phase] }} · </span>
        {{ ({queued:'排队',preparing:'准备风场',searching:'搜索中',found:'已找到完整路线',unconfirmed:'尚未确认',data_insufficient:'数据不足',resource_limit:'资源受限',no_feasible_route:'未找到可行路线',cancelled:'已取消'})[attempt.status] || attempt.status }}
        <span class="attempt-timing" v-if="attempt.elapsed_seconds != null"> · 排队 {{ (attempt.queue_wait_seconds || 0).toFixed(1) }}s · 准备 {{ attempt.preparation_seconds == null ? '—' : attempt.preparation_seconds.toFixed(1)+'s' }} · 搜索 {{ attempt.search_seconds == null ? '—' : attempt.search_seconds.toFixed(1)+'s' }}</span></p>
    </details>
  </section>
</template>
<style scoped>
.recommendation-results { margin-bottom:16px; padding:14px; border-radius:8px; background:#111f2d; color:#fff; }
.recommendation-results h2 { font-size:16px; margin:0; }
.requested-departure { color:#fff; }
.recommendation-card { border-left:4px dashed var(--recommendation-color); padding:12px; margin-top:12px; background:#0b1723; border-radius:6px; }
.recommendation-card strong { color:var(--recommendation-color); }
.selectable-card { cursor:pointer; outline:1px solid transparent; transition:outline-color .15s,background .15s; }
.selectable-card:hover { background:#12273a; }
.selectable-card:focus-visible { outline:2px solid #65d7f0; outline-offset:2px; }
.selected-card { outline-color:#65d7f0; background:#122b3d; }
.selected-badge,.switching-badge { display:inline-block; margin:8px 8px 0 0; padding:3px 8px; border-radius:4px; font-size:12px; }
.selected-badge { color:#fff; background:#17617c; }
.switching-badge { color:#fff; background:#34485a; }
.recommended-windows { list-style:none; padding:0; color:#b6f7d0; }
.recommended-windows li { padding:5px 0; font-size:14px; }
.recommendation-benefits { font-size:12px; color:#dce9f3; }
</style>
