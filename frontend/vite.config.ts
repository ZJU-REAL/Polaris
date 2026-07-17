import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          // 框架层单独成 chunk：业务代码迭代时用户仍可命中长效缓存
          if (/node_modules\/(react|react-dom|react-router|react-router-dom|@remix-run|scheduler)\//.test(id)) {
            return 'vendor-react';
          }
          if (id.includes('node_modules/katex/')) return 'vendor-katex';
          if (/node_modules\/(@codemirror|codemirror|yjs|y-codemirror\.next|y-protocols|lib0|style-mod|w3c-keyname|crelt)\//.test(id)) {
            return 'vendor-editor';
          }
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // 本地 dev 默认打宿主机后端；docker dev compose 里覆盖为 http://api:8000
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
        ws: true, // /ws/notifications WebSocket 代理
      },
    },
  },
});
