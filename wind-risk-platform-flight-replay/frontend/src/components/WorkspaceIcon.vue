<script setup>
defineProps({ name: String })

const paths = {
  flight: ['M12 3 21 20l-9-5-9 5L12 3Z', 'M12 3v12'],
  planning: ['M5 5h8M5 9h5M5 13h3', 'M14 20h6v-6', 'M10 20l10-10', 'M3 3h14v5M3 3v18h4'],
  decision: ['M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4Z', 'm8 12 3 3 5-6'],
  weather: ['M3 8h12a3 3 0 1 0-3-3', 'M3 12h16', 'M3 16h9a3 3 0 1 1-3 3'],
  specified: ['M5 4h14v16H5V4Z', 'M8 2v4M16 2v4M5 9h14', 'm9 14 2 2 4-4'],
  recommended: ['M12 3a9 9 0 1 0 9 9', 'M12 7v5l3 2', 'm18 2 1 3 3 1-3 1-1 3-1-3-3-1 3-1 1-3Z'],
  history: ['M3 11a9 9 0 1 1 2 7', 'M3 5v6h6', 'm11 8 5 4-5 4V8Z'],
  settings: ['M4 6h16M4 12h16M4 18h16', 'M8 3v6M16 9v6M10 15v6'],
}
</script>

<template>
  <svg class="workspace-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path v-for="(path, index) in paths[name] || paths.flight" :key="index" :d="path" /></svg>
</template>
