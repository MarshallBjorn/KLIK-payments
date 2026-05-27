import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

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
    port: 5174,
    allowedHosts,
    host: true,
  },
})
