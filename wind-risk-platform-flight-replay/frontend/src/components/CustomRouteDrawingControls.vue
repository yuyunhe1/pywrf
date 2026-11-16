<script setup>
import { computed } from 'vue'
import { customRoutePayload } from '../utils/customRouteDrawing'

const props = defineProps({ draft: { type: Object, required: true }, selection: { type: Object, required: true }, speed: Number,
  purpose: {type:String,default:'fixed'} })
defineEmits(['rename', 'undo', 'clear', 'cancel', 'save'])
const validation = computed(() => {
  try { customRoutePayload(props.draft, props.selection); return '' } catch (reason) { return reason.message }
})
</script>

<template>
  <section v-if="draft.active" class="custom-route-controls" aria-label="自定义绘制航线" :aria-busy="draft.saving">
    <div class="custom-route-heading">
      <strong>{{ purpose === 'departure_range' ? '绘制必经航点' : '自定义绘制航线' }}</strong>
      <span>{{ draft.points.length }} 个航点</span>
      <span>{{ selection.source?.toUpperCase() }} · {{ selection.level }} · {{ speed }} m/s</span>
    </div>
    <label class="custom-route-name">航线名称
      <input type="text" aria-label="自定义航线名称" :value="draft.name" :disabled="draft.saving" maxlength="120"
        @input="$emit('rename', $event.target.value)" />
    </label>
    <div class="custom-route-actions">
      <button class="ghost" :disabled="draft.saving || !draft.points.length" @click="$emit('undo')">撤销上一点</button>
      <button class="ghost" :disabled="draft.saving || !draft.points.length" @click="$emit('clear')">清空</button>
      <button class="ghost" :disabled="draft.saving" @click="$emit('cancel')">取消</button>
      <button class="primary" :disabled="draft.saving || Boolean(validation)" :title="validation" @click="$emit('save')">
        <span v-if="draft.saving" class="loading-spinner"></span>{{ draft.saving ? '确认中…' : purpose === 'departure_range' ? '完成绘制并推荐' : '完成绘制并评估' }}
      </button>
    </div>
    <p v-if="draft.error" class="custom-route-error" role="alert">{{ draft.error }}</p>
  </section>
</template>
