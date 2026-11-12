import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'GEOPILOT_')
  return {
    server: { proxy: { '/api': env.GEOPILOT_API_URL ?? 'http://127.0.0.1:8000' } },
    test: { environment: 'jsdom' },
  }
})
