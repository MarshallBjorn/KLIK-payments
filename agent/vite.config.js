import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const proxyTarget = process.env.VITE_PROXY_TARGET || 'http://localhost:8000'

const rawAllowedHosts = process.env.VITE_ALLOWED_HOSTS || 'localhost,127.0.0.1'
const allowedHosts =
  rawAllowedHosts.trim() === 'all'
    ? true
    : rawAllowedHosts
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 5175,
    allowedHosts,
    proxy: {
      '/api': {
        target: proxyTarget,
        changeOrigin: true,
      },
    },
  },
})
