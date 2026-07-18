import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { cp } from 'node:fs/promises';

// pdf.js 要靠 cMap（CID/CJK 字体映射）与标准字体数据才能把字形画到 canvas 上；
// 缺这两份资源时整页渲染为空白（文本层仍可选中，但看不到内容）。
// 把它们从 pdfjs-dist 拷到 public/pdfjs（gitignore），dev 启动与 build 时各拷一次，
// 运行时按 /pdfjs/cmaps、/pdfjs/standard_fonts 提供——见 PdfReader 的 PDF_OPTIONS。
function copyPdfAssets(): Plugin {
  const run = async () => {
    for (const dir of ['cmaps', 'standard_fonts']) {
      await cp(`node_modules/pdfjs-dist/${dir}`, `public/pdfjs/${dir}`, { recursive: true }).catch(
        (e) => console.warn(`[copy-pdf-assets] ${dir} 拷贝失败:`, e?.message),
      );
    }
  };
  return { name: 'copy-pdf-assets', buildStart: run, configureServer: run };
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
