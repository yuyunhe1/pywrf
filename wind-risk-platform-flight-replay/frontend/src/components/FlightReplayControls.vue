<script setup>
import { computed } from 'vue'
import { formatReplayTime } from '../utils/flightReplay'
const props = defineProps({ replay: Object })
defineEmits(['play', 'pause', 'seek', 'rate-change', 'mode-change', 'retry'])
const flightTime = computed(() => {
  const departure = Date.parse(props.replay?.result?.departure_at)
  return Number.isFinite(departure) ? new Date(departure + props.replay.seconds * 1000).toISOString() : null
})
</script>

<template>
  <section v-if="replay?.active" class="flight-replay-controls" aria-label="飞行回放">
    <div class="replay-heading">
      <strong>{{ replay.mode === 'flight' ? '飞行过程回放' : '单时刻剖面' }}</strong>
      <div class="replay-mode-switch">
        <button :class="{ active: replay.mode === 'flight' }" :aria-pressed="replay.mode === 'flight'" @click="$emit('mode-change', 'flight')">飞行过程</button>
        <button :class="{ active: replay.mode === 'snapshot' }" :aria-pressed="replay.mode === 'snapshot'" @click="$emit('mode-change', 'snapshot')">单时刻</button>
      </div>
    </div>
    <p v-if="replay.notice" class="replay-notice" role="status">{{ replay.notice }}</p>
    <template v-if="replay.mode === 'flight'">
      <p v-if="replay.loading" role="status">正在计算沿途经过时间与风况…</p>
      <p v-else-if="replay.error" class="replay-notice" role="alert">{{ replay.error }} <button class="inline-button" @click="$emit('retry')">重试</button></p>
      <template v-else-if="replay.result">
        <div class="replay-time-row" aria-label="北京时间">
          <span title="飞行时刻（北京时间）">飞行 <b>{{ formatReplayTime(flightTime) }}</b></span>
          <span title="底图风场时刻（北京时间）">风场 <b>{{ formatReplayTime(replay.mapTime) }}</b></span>
          <span v-if="replay.mapLoading" role="status">加载风场中…</span>
        </div>
        <div class="replay-timeline-row">
          <button class="replay-play" :disabled="replay.mapLoading && !replay.playing" :aria-label="replay.playing ? '暂停回放' : '播放航程'" @click="$emit(replay.playing ? 'pause' : 'play')">{{ replay.playing ? '暂停' : '播放' }}</button>
          <input aria-label="飞行回放进度" type="range" min="0" :max="replay.result.flight_duration_seconds" step="0.001" :value="replay.seconds" @input="$emit('seek', Number($event.target.value))">
          <select aria-label="回放倍速" :value="replay.rate" @change="$emit('rate-change', Number($event.target.value))">
            <option v-for="rate in [10, 30, 60, 120, 600]" :key="rate" :value="rate">{{ rate }}×</option>
          </select>
        </div>
        <details class="replay-details"><summary>回放详情</summary>
        <div class="replay-time-row subtle"><span>起飞 {{ formatReplayTime(replay.result.departure_at) }}</span><span>到达 {{ formatReplayTime(replay.result.arrival_at) }}</span></div>
        <p v-if="replay.result.wind_time_step_seconds" class="replay-legend">计算风场每 {{ replay.result.wind_time_step_seconds / 60 }} 分钟切换一帧，帧内保持不变。</p>
        <p v-if="replay.mapUpdateIntervalSeconds" class="replay-legend">粒子流底图每飞行 {{ replay.mapUpdateIntervalSeconds / 60 }} 分钟更新一次。</p>

        <p class="replay-legend">蓝色粗线：规划航线 · 风险详见右侧通航决策<br>浅色虚线：已飞过航段 · 白色飞机：预计位置</p>
        </details>
        <p v-if="replay.mapError" class="replay-notice" role="status">{{ replay.mapError }}</p>
      </template>
    </template>
    <p v-else class="replay-legend">底图与曲线均使用左侧选择的风场时刻。可切回飞行过程，查看飞机经过时遇到的风。</p>
  </section>
</template>
