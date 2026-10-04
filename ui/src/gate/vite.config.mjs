// Админка гейта (GW-HARNESS-02): отдельная сборка, основной бандл роутера не затрагивается.
//   npx vite build --config src/gate/vite.config.mjs
// Результат — atlas_gate/gate/static, его отдаёт гейт с /harness/admin/ (только локально и
// только при ATLAS_GATE_ENABLED). `npm run build` без этой команды даёт прежние хэши.
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const here = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig({
  root: here,
  base: './',
  plugins: [vue()],
  build: {
    outDir: fileURLToPath(new URL('../../../atlas_gate/gate/static', import.meta.url)),
    emptyOutDir: true,
  },
})
