import { defineConfig, type Plugin, type ProxyOptions } from 'vite';
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
      // dereference: pnpm 下 node_modules/pdfjs-dist 是指向 store 的符号链接，
      // 默认拷贝会把链接原样复制进 public/，打包后成为悬空链接。
      cpSync(`node_modules/pdfjs-dist/${dir}`, `public/pdfjs/${dir}`, {
        recursive: true,
        dereference: true,
      });
    } catch (e) {
      console.warn(`[copy-pdf-assets] ${dir} 拷贝失败:`, (e as Error)?.message);
    }
  }
  return { name: 'copy-pdf-assets' };
}

/**
 * 开发服务器把 /api 同源转给引擎。浏览器对同源 POST 也会带 Origin（如
 * http://localhost:5173），而引擎（#850）在没有桌面会话口令时只给「不带 Origin 或
 * Origin 在白名单里」的请求发本地会话。对引擎而言这本来就是同源请求：只去掉
 * 「Origin 恰好是开发服务器自己」的这一种；别的网站发来的 Origin 原样转发、照样被拒。
 * 这样 make frontend-dev 不用配 POLARIS_CORS_ORIGINS 就能用。
 */
const dropSameOriginHeader: NonNullable<ProxyOptions['configure']> = (proxy) => {
  proxy.on('proxyReq', (proxyReq, req) => {
    const { origin, host } = req.headers;
    if (origin && host && (origin === `http://${host}` || origin === `https://${host}`)) {
      proxyReq.removeHeader('origin');
    }
  });
};

export default defineConfig({
  plugins: [react(), copyPdfAssets()],
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
        // 本地 dev 默认打宿主机后端（make backend-dev）。changeOrigin 把 Host 改成目标地址，
        // 引擎的 Host 白名单（#850）只认回环名字：目标若不是 localhost/127.0.0.1，
        // 引擎那边要设 POLARIS_ALLOWED_HOSTS=<目标主机名>
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
        configure: dropSameOriginHeader,
      },
      '/ws': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
        ws: true, // /ws/notifications WebSocket 代理
      },
      '/mcp': {
        // MCP 协议端点（POST /mcp，JSON-RPC）——见 docs/mcp.md
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
        configure: dropSameOriginHeader,
      },
    },
  },
});
