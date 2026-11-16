<script setup>
defineProps({ side: String, title: String, width: Number, visible: Boolean, compact: Boolean })
defineEmits(['close', 'resize-start', 'resize-key'])
</script>

<template>
  <aside v-show="visible" class="panel workspace-panel" :class="side === 'left' ? 'control-panel' : 'analysis-panel'"
    :style="{ width: `${width}px` }" :aria-label="title">
    <div class="panel-heading"><h1>{{ title }}</h1>
      <button class="panel-close ghost" :aria-label="`收起${title}`" @click="$emit('close')">收起 <span aria-hidden="true">{{ side === 'left' ? '‹' : '›' }}</span></button>
    </div>
    <slot />
    <div v-if="!compact" class="panel-resizer" :class="side" role="separator" aria-orientation="vertical"
      :aria-label="`调整${title}宽度`" :aria-valuenow="Math.round(width)" aria-valuemin="280" aria-valuemax="480" tabindex="0"
      @pointerdown="$emit('resize-start', $event)" @keydown="$emit('resize-key', $event)"></div>
  </aside>
</template>
