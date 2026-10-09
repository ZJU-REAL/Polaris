/* ============================================================
   本地会话（#842）。

   Polaris 只在本机运行、只有一个用户：没有登录页、没有账号。会话只有一个来源——
   POST /auth/local-session，引擎幂等地确保本地用户存在并签发 token。启动时
   （路由守卫发现没有 token）和会话失效（任何请求撞到 401）时都走这里。

   这里刻意用裸 fetch 而不是 lib/api.ts 的 request：
   ① 这个端点无鉴权，不需要 Bearer/本地路由层；
   ② api.ts 的 401 拦截会调用本模块（handleUnauthorized），反向依赖会成环。
   ============================================================ */

import { apiBase } from './endpoint';
import { writeToken } from './token-store';

/** POST /auth/local-session（无 body、无鉴权）。引擎连不上或出错时抛错。 */
export async function requestLocalSessionToken(): Promise<string> {
  const res = await fetch(`${apiBase()}/auth/local-session`, { method: 'POST' });
  if (!res.ok) throw new Error(`local-session HTTP ${res.status}`);
  const data = (await res.json()) as { access_token: string; token_type: string };
  return data.access_token;
}

/** 取本地会话并落存储；失败返回 null（调用方显示「引擎没连上」页）。 */
export async function acquireLocalSession(): Promise<string | null> {
  try {
    const token = await requestLocalSessionToken();
    writeToken(token);
    return token;
  } catch {
    return null;
  }
}

/* —— 401 统一处理 ——
   api.ts 三个请求封装（request/requestBlob/requestStream）撞到 401 时调这里：
   重取会话并整页刷新，让所有查询带新会话重来。取不到（引擎没连上）就清掉失效的
   token 再刷新——路由守卫会再试一次，仍失败就显示带重试的兜底页。并发 401 只触发一次。 */
let recovering = false;

export function handleUnauthorized(): void {
  if (typeof window === 'undefined') return;
  if (recovering) return;
  recovering = true;
  void (async () => {
    const token = await acquireLocalSession();
    if (!token) writeToken(null);
    window.location.reload();
  })();
}
