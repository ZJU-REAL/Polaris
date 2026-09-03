/* ============================================================
   desktop 档位免登录（P1-A3）。

   后端 /auth/capabilities 返回 local_session=true 时，前端不渲染登录页：
   启动（或路由守卫判定未登录）时直接 POST /auth/local-session 换会话 token。
   server 档位该端点结构性 404，一切失败都静默回落到现有登录页流程。

   这里刻意用裸 fetch 而不是 lib/api.ts 的 request：
   ① 两个端点都无鉴权，不需要 Bearer/本地路由层；
   ② api.ts 的 401 拦截会调用本模块（handleUnauthorized），反向依赖会成环。
   ============================================================ */

import { apiBase } from './endpoint';
import { writeToken } from './token-store';

/** local_session 能力的模块级缓存：部署档位在进程生命周期内不会变。 */
let localSessionCached: boolean | null = null;

/** 查询后端是否支持免登录（结果记忆化；网络失败按 false 处理并同样缓存）。 */
export async function detectLocalSession(): Promise<boolean> {
  if (localSessionCached !== null) return localSessionCached;
  try {
    const res = await fetch(`${apiBase()}/auth/capabilities`);
    if (!res.ok) {
      localSessionCached = false;
    } else {
      const data = (await res.json()) as { local_session?: boolean };
      localSessionCached = data.local_session === true;
    }
  } catch {
    localSessionCached = false;
  }
  return localSessionCached;
}

/** POST /auth/local-session（无 body、无鉴权）。server 档位 404 时抛错。 */
export async function requestLocalSessionToken(): Promise<string> {
  const res = await fetch(`${apiBase()}/auth/local-session`, { method: 'POST' });
  if (!res.ok) throw new Error(`local-session HTTP ${res.status}`);
  const data = (await res.json()) as { access_token: string; token_type: string };
  return data.access_token;
}

/** 取本地会话并按现有惯例落存储；失败返回 null（调用方回落登录页）。 */
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
   免登录模式下重取会话并整页刷新（而不是踢去登录页）；其余情况维持原行为跳
   /login。并发 401 只触发一次。 */
let recovering = false;

export function handleUnauthorized(): void {
  if (typeof window === 'undefined') return;
  if (recovering) return;
  recovering = true;
  void (async () => {
    if (await detectLocalSession()) {
      const token = await acquireLocalSession();
      if (token) {
        // token 已落存储，整页刷新让所有查询带新会话重来
        window.location.reload();
        return;
      }
    }
    if (window.location.pathname !== '/login') {
      window.location.assign('/login');
      return;
    }
    recovering = false;
  })();
}
