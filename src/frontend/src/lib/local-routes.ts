/* ============================================================
   本地 / 远端路由表。

   lib/api.ts 有近 4000 行，但所有 REST 调用都收口在 request / requestBlob 两个
   函数里，所以「这次调用走本地还是走服务器」只需要在那两处决策，**调用点一处
   都不用改**。备选方案（每个 call site 写 if、包一层 facade 重导出 200+ 方法、
   Proxy 包 api 对象）都要按能力数量反复改动，或者破坏类型推导。

   一期 ROUTES 是空表：没有任何本地能力，行为与改造前完全一致。
   第二期加本地编译 = 往表里加一行 + 在 agent 侧加一个 handler。
   ============================================================ */

import { isCapabilityAvailable } from './host';

/** 唯一允许静默回落到服务器的异常。 */
export class LocalUnavailable extends Error {
  constructor(message = 'local capability unavailable') {
    super(message);
    this.name = 'LocalUnavailable';
  }
}

export interface LocalContext {
  method: string;
  path: string;
  /** 从 :name 占位符里抓出来的参数。 */
  params: Record<string, string>;
  init: RequestInit;
}

export type LocalHandler = (ctx: LocalContext) => Promise<unknown>;

interface Route {
  capability: string;
  handler: LocalHandler;
}

/**
 * key = `${METHOD} ${pattern}`，pattern 用 :name 占位。
 * 第二期示例：
 *   'POST /manuscripts/:id/compile': { capability: 'latex.compile', handler: compileLocally },
 *   'GET /manuscripts/:id/pdf':      { capability: 'latex.compile', handler: localPdfIfFresh },
 */
const ROUTES: Record<string, Route> = {};

/* —— 熔断：同一能力连续失败若干次后，本会话内不再尝试 ——
   否则本地进程崩了会变成「每次请求都先白等一轮再回落」。 */
const FAILURE_LIMIT = 3;
const failures = new Map<string, number>();

export function noteLocalFailure(capability: string): boolean {
  const next = (failures.get(capability) ?? 0) + 1;
  failures.set(capability, next);
  return next >= FAILURE_LIMIT;
}

export function isCircuitOpen(capability: string): boolean {
  return (failures.get(capability) ?? 0) >= FAILURE_LIMIT;
}

export function resetCircuit(capability: string): void {
  failures.delete(capability);
}

function matchPattern(pattern: string, path: string): Record<string, string> | null {
  const want = pattern.split('/');
  const got = path.split('/');
  if (want.length !== got.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < want.length; i += 1) {
    const w = want[i] ?? '';
    const g = got[i] ?? '';
    if (w.startsWith(':')) {
      if (!g) return null;
      params[w.slice(1)] = decodeURIComponent(g);
    } else if (w !== g) {
      return null;
    }
  }
  return params;
}

/** 找到可用的本地实现；没有则返回 null（调用方直接走服务器）。 */
export function resolveLocalHandler(
  method: string,
  path: string,
): { route: Route; params: Record<string, string> } | null {
  if (Object.keys(ROUTES).length === 0) return null; // 一期快速路径
  const pathOnly = path.split('?')[0] ?? path;
  for (const [key, route] of Object.entries(ROUTES)) {
    const sep = key.indexOf(' ');
    if (key.slice(0, sep) !== method) continue;
    const params = matchPattern(key.slice(sep + 1), pathOnly);
    if (!params) continue;
    if (isCircuitOpen(route.capability)) return null;
    if (!isCapabilityAvailable(route.capability)) return null;
    return { route, params };
  }
  return null;
}
