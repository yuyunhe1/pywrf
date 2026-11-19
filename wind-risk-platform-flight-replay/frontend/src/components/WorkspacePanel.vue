<script setup>
import WorkspaceIcon from './WorkspaceIcon.vue'
defineProps({ side: String, title: String, width: Number, visible: Boolean, compact: Boolean })
defineEmits(['toggle'])
</script>

<template>
  <aside v-show="visible" class="panel workspace-panel" :class="side === 'left' ? 'control-panel' : 'analysis-panel'"
    :id="`workspace-panel-${side}`" :style="{ width: `${width}px` }" :aria-label="title">
    <div class="panel-heading"><span class="panel-heading-icon"><WorkspaceIcon :name="side === 'left' ? 'planning' : 'decision'" /></span><h1>{{ title }}</h1><span class="panel-heading-detail" aria-hidden="true"><i></i><i></i><i></i></span></div>
    <slot />
  </aside>
  <button class="panel-toggle" :class="[side, { 'is-open': visible }]"
    :style="{ [side]: `${visible ? width : 0}px` }"
    :aria-label="`${visible ? '收起' : '展开'}${title}`" :title="`${visible ? '收起' : '展开'}${title}`"
    :aria-expanded="visible" :aria-controls="`workspace-panel-${side}`" @click="$emit('toggle')">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path :d="(side === 'left') === visible ? 'M14 6l-6 6 6 6' : 'M10 6l6 6-6 6'" /></svg>
  </button>
</template>
