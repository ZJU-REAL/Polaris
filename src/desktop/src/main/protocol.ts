/* ============================================================
   app://polaris 自定义协议 —— 页面加载方式。

   为什么不是 file://：Chromium 把 file:// 当 opaque origin，
   `new Worker('file:///...')` 会被直接拒绝，而 pdf.js 阅读器依赖 worker，
   PDF 阅读页会完全不可用；且 /assets/、/pdfjs/cmaps/ 这些绝对路径全要改，
   桌面端产物会与 web 分叉。
   为什么不是本机 HTTP server：本机任意进程都能访问那个端口，凭空多一个
   网络攻击面，Windows 上还会弹防火墙。

   注册成 standard + secure 之后，页面有真正的 origin（app://polaris），
   pushState / Worker / localStorage / navigator.clipboard / Notification
   全部按 https 页面的规则工作 —— 前端一行结构代码都不用改。

   注意：scheme 用 app://，polaris:// 留给深链接，避免同一个 scheme 既做
   内容协议又在系统里注册成外部 handler。
   ============================================================ */

import { app, net, protocol } from 'electron';
import { existsSync, statSync } from 'node:fs';
import { extname, join, normalize, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

export const APP_SCHEME = 'app';
export const APP_HOST = 'polaris';
export const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`;
export const APP_INDEX = `${APP_ORIGIN}/`;

/** 必须在 app.whenReady() 之前调用。 */
export function registerAppScheme(): void {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: APP_SCHEME,
      privileges: {
        standard: true, // 真 origin：pushState / Worker / localStorage
        secure: true, // secure context：clipboard / Notification / crypto.subtle
        supportFetchAPI: true,
        corsEnabled: true,
        stream: true, // Range 请求：写作页的 <iframe src=blob:…pdf> 与媒体
      },
    },
  ]);
}

function rendererRoot(): string {
  // 打包后 electron-builder 把 src/frontend/dist 放进 resources/renderer；
  // 本地 `npm run dev` 直接读同仓库的 frontend 构建产物。
  return app.isPackaged
    ? join(process.resourcesPath, 'renderer')
    : join(__dirname, '..', '..', 'frontend', 'dist');
}

/**
 * CSP 随当前服务器地址动态生成（换服务器后重建窗口即刷新）。
 * 两处宽松是必需的，不要「顺手收紧」：
 * - script-src 'wasm-unsafe-eval'：pdf.js 的 openjpeg / qcms 是 WASM；
 * - style-src 'unsafe-inline'：CodeMirror 的 style-mod 与 KaTeX 在运行时
 *   insertRule 注入样式，去掉编辑器和公式渲染会直接崩。
 */
export function buildCsp(serverUrl: string): string {
  const connect = new Set<string>(["'self'"]);
  if (serverUrl) {
    try {
      const u = new URL(serverUrl);
      connect.add(u.origin);
      connect.add(u.origin.replace(/^http/, 'ws'));
    } catch {
      /* 地址非法时只留 'self'，前端会停在配置页 */
    }
  }
  return [
    "default-src 'self'",
    "script-src 'self' 'wasm-unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "img-src 'self' data: blob:",
    'media-src \'self\' blob:',
    'frame-src blob:',
    "worker-src 'self' blob:",
    `connect-src ${[...connect].join(' ')}`,
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ].join('; ');
}

export function handleAppProtocol(getServerUrl: () => string): void {
  const root = normalize(rendererRoot());

  protocol.handle(APP_SCHEME, async (request) => {
    const url = new URL(request.url);
    const pathname = decodeURIComponent(url.pathname);
    let filePath = normalize(join(root, pathname));

    // 路径穿越防护：归一化后必须仍在 renderer 根目录内
    if (filePath !== root && !filePath.startsWith(root + sep)) {
      return new Response('Forbidden', { status: 403 });
    }

    const isFile = existsSync(filePath) && statSync(filePath).isFile();
    if (!isFile) {
      // SPA fallback：带扩展名的当作真实资源缺失（404），其余交给前端路由。
      // 这样 createBrowserRouter 与 api.ts 的 location.assign('/login') 原样可用。
      if (extname(pathname)) return new Response('Not Found', { status: 404 });
      filePath = join(root, 'index.html');
      if (!existsSync(filePath)) {
        return new Response('renderer not built — run `npm run build` in src/frontend', {
          status: 500,
        });
      }
    }

    const res = await net.fetch(pathToFileURL(filePath).toString());
    const headers = new Headers(res.headers);
    if (filePath.endsWith('.html')) {
      headers.set('Content-Security-Policy', buildCsp(getServerUrl()));
    }
    return new Response(res.body, { status: res.status, headers });
  });
}
