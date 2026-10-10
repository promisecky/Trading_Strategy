import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 子路径部署: VITE_APP_BASE=/resonance npm run build (默认根路径, 本地开发无需配置)
const appBase = process.env.VITE_APP_BASE || ''

export default defineConfig({
  plugins: [react()],
  base: appBase || '/',
  define: {
    __APP_BASE__: JSON.stringify(appBase),
  },
  server: {
    port: 5174,
    host: '0.0.0.0',
    allowedHosts: ['.opensesame.cc', 'localhost', '127.0.0.1'],
    proxy: {
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: true,
      },
    },
  },
  // 生产预览: 服务 dist 构建产物(打包后首屏请求数从数百降到3)。
  // 端口/host/代理与 dev 一致, systemd 由 `vite dev` 切到 `vite preview` 后,
  // SSH 隧道/CF/新加坡反代/前端调用均无需改动。
  preview: {
    port: 5174,
    host: '0.0.0.0',
    allowedHosts: ['.opensesame.cc', 'localhost', '127.0.0.1'],
    proxy: {
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: true,
      },
    },
  },
})
