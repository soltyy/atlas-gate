import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// Сборка складывается прямо в статику бэкенда (FastAPI отдаёт её с «/»).
// base: './' — пути к ассетам относительные, чтобы страница работала
// и с корня, и из-под любого префикса.
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: '../atlas_gate/gate/static',
    emptyOutDir: true,
  },
  server: {
    // В режиме разработки запросы к API уходят на локальный роутер.
    proxy: {
      '/harness': 'http://127.0.0.1:8766',
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
  },
})
