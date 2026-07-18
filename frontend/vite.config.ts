import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { cpSync } from 'node:fs';

// pdf.js 要靠 cMap（CID/CJK 字体映射）与标准字体数据才能画出某些字体的字形。
// 把它们从 pdfjs-dist 拷到 public/pdfjs（gitignore），运行时按 /pdfjs/cmaps、
// /pdfjs/standard_fonts 提供——见 PdfReader 的 PDF_OPTIONS。
// 必须在插件实例化时（config 加载阶段、静态服务建立前）同步拷完，否则 vite 的
// 静态中间件会先于拷贝就绪、对未就位的文件走 SPA fallback 返回 index.html。
function copyPdfAssets(): Plugin {
  for (const dir of ['cmaps', 'standard_fonts']) {
    try {
      cpSync(`node_modules/pdfjs-dist/${dir}`, `public/pdfjs/${dir}`, { recursive: true });
    } catch (e) {
      console.warn(`[copy-pdf-assets] ${dir} 拷贝失败:`, (e as Error)?.message);
    }
  }
  return { name: 'copy-pdf-assets' };
}

export default defineConfig({
  plugins: [react(), copyPdfAssets()],
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
