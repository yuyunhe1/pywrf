import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        // Keep vendor transforms small. This also avoids esbuild's large-input
        // temporary-file transport, which is restricted on some Windows hosts.
        manualChunks(id) {
          const path = id.replaceAll('\\', '/')
          if (path.includes('/node_modules/element-plus/es/components/')) {
            const component = path.split('/element-plus/es/components/')[1].split('/')[0]
            return `ui-${component.startsWith('select') ? 'select' : component === 'table' ? 'table' : 'common'}`
          }
          if (path.includes('/node_modules/echarts/')) return 'charts'
          if (path.includes('/node_modules/zrender/')) return 'chart-renderer'
          if (path.includes('/node_modules/@vue/') || path.includes('/node_modules/vue/')) return 'vue'
        },
      },
    },
  },
  preview: {
    host: '0.0.0.0', port: Number(process.env.FRONTEND_PORT || 5174), strictPort: true,
    allowedHosts: ['.trycloudflare.com'],
    proxy: {
      '/api': { target: `http://127.0.0.1:${process.env.BACKEND_PORT || 8001}`, ws: true, timeout: 7260000, proxyTimeout: 7260000 },
    },
  },
  server: {
    allowedHosts: ['.trycloudflare.com'],
    watch: { ignored: ['**/*.LICENSE.md', '**/public/licenses/**'] },
    port: Number(process.env.FRONTEND_PORT || 5174),
    strictPort: true,
    proxy: {
      '/api': { target: `http://127.0.0.1:${process.env.BACKEND_PORT || 8001}`, ws: true, timeout: 7260000, proxyTimeout: 7260000 },
    },
  },
})
