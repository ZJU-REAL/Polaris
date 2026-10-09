/* ============================================================
   运行时端点解析 —— 桌面端与浏览器开发态的唯一分歧点。

   桌面端：Electron preload 在任何 renderer 脚本执行前同步注入
           window.__POLARIS__；请求全部走本机引擎（127.0.0.1）。
   浏览器（vite 开发服务器）：window.__POLARIS__ 不存在，一切退化为
           同源相对路径（apiBase() === '/api'），由开发代理转给后端。

   业务代码只 import 本文件，不直接读 window.__POLARIS__。
   ============================================================ */

import { kernelLocalBackend } from './host';

/** Electron preload 注入的宿主运行时信息（web 端为 undefined）。 */
export interface PolarisHostRuntime {
  platform: 'darwin' | 'win32' | 'linux';
  appVersion: string;
}

declare global {
  interface Window {
    __POLARIS__?: PolarisHostRuntime;
  }
}

function hostRuntime(): PolarisHostRuntime | undefined {
  return typeof window === 'undefined' ? undefined : window.__POLARIS__;
}

/** 是否运行在桌面客户端里。 */
export function isDesktop(): boolean {
  return hostRuntime() != null;
}

/* —— 本地引擎（P1-A4）——
   桌面内核在本机拉起 Python 后端。启动时经宿主桥问一次 kernel.localBackend，
   模块级缓存结果：REST/WS 全部走 127.0.0.1。拿不到地址时 App 显示「本机
   引擎没有启动」页。web 端没有宿主桥，探测直接短路成 null——零请求。 */

let localBase: string | null = null;
let localProbe: Promise<string | null> | null = null;

/**
 * 探测本地引擎地址（幂等，结果缓存）。main.tsx 在桌面端挂载前 await 它，
 * 保证首屏请求就走对地址。
 */
export function probeLocalBackend(): Promise<string | null> {
  localProbe ??= (async () => {
    try {
      const info = await kernelLocalBackend();
      localBase = info?.baseUrl?.replace(/\/+$/, '') || null;
    } catch {
      // 探测失败按「无本地引擎」处理；不让一次 IPC 故障卡死首屏
      localBase = null;
    }
    return localBase;
  })();
  return localProbe;
}

/** 本地引擎 origin（如 http://127.0.0.1:18080）；未探测到时为 null。 */
export function localOrigin(): string | null {
  return localBase;
}

/**
 * 后端 origin：桌面端为本机引擎地址，web 端为 ''（同源，走相对路径）。
 * 故意做成函数而非模块级常量：避免依赖「探测早于本模块求值」这一隐式时序。
 */
export function serverOrigin(): string {
  return localBase ?? '';
}

/** REST / SSE 的基址。web 端返回 '/api'，与改造前的字面量完全一致。 */
export function apiBase(): string {
  return `${serverOrigin()}/api`;
}

/**
 * WebSocket 完整 URL（path 需以 / 开头，可带 query）。
 * web 端逐字保留改造前的 window.location 推导逻辑。
 */
export function wsUrl(path: string): string {
  const origin = serverOrigin();
  if (origin) return `${origin.replace(/^http/, 'ws')}${path}`;
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${window.location.host}${path}`;
}

/**
 * 给本机其他程序（MCP 客户端、浏览器扩展）填的完整地址：桌面端是本机引擎
 * origin；浏览器里开发调试时是页面自己的 origin（开发代理会转给后端）。
 */
export function engineOrigin(): string {
  return localBase ?? (typeof window === 'undefined' ? '' : window.location.origin);
}
