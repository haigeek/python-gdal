import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发：npm run dev（5173）把 /api 代理到本地后端 8000
// 构建：产物输出到 ../gdb2pg/web/static，由 python -m gdb2pg web 静态托管
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: '../gdb2pg/web/static',
    emptyOutDir: true,
  },
})