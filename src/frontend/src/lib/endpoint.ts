/* ============================================================
   运行时端点解析 —— web 与桌面端的唯一分歧点。

   web 端：window.__POLARIS__ 不存在，一切退化为同源相对路径，
           行为与改造前逐字等价（apiBase() === '/api'）。
   桌面端：Electron preload 在任何 renderer 脚本执行前同步注入
           window.__POLARIS__，其中 serverUrl 是用户配置的服务器地址。

   业务代码只 import 本文件，不直接读 window.__POLARIS__。
   ============================================================ */

/** Electron preload 注入的宿主运行时信息（web 端为 undefined）。 */
export interface PolarisHostRuntime {
  /** 用户配置的服务器地址，如 https://polaris.example.edu（可带尾斜杠，本模块会归一化）。 */
  serverUrl: string;
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

/**
 * 服务器 origin：web 端为 ''（同源，走相对路径），桌面端为配置的地址（已去尾斜杠）。
 * 故意做成函数而非模块级常量：避免依赖「注入早于本模块求值」这一隐式时序。
 */
export function serverOrigin(): string {
  return (hostRuntime()?.serverUrl ?? '').replace(/\/+$/, '');
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
 * 面向「别人也能打开」的链接：邀请/分享链接、给外部 MCP 客户端的服务端点。
 * 收件人多数用浏览器打开，所以桌面端要指向服务器上的 web 门户，而不是 app:// 本地页面。
 */
export function portalUrl(path = ''): string {
  const origin = serverOrigin() || (typeof window === 'undefined' ? '' : window.location.origin);
  return `${origin}${path}`;
}
