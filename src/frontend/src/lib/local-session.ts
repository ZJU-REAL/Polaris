/* ============================================================
   本地会话（#842）。

   Polaris 只在本机运行、只有一个用户：没有登录页、没有账号。会话只有一个来源——
   POST /auth/local-session，引擎幂等地确保本地用户存在并签发 token。启动时
   （路由守卫发现没有 token）和会话失效（请求撞到「会话无效」的 401）时都走这里。

   这个端点不是谁都能调（#850）：桌面外壳每次启动生成一个会话口令，只交给引擎和
   本界面（kernel.localBackend 的 sessionSecret），请求时放进 X-Polaris-Session-Secret
   头；别的网页拿不到它，也就换不到会话。浏览器开发态没有口令，引擎改按 Origin 判断。

   这里刻意用裸 fetch 而不是 lib/api.ts 的 request：
   ① 这个端点不用 Bearer/本地路由层；
   ② api.ts 的 401 拦截会调用本模块（handleUnauthorized），反向依赖会成环。
   ============================================================ */

import { apiBase, localSessionSecret } from './endpoint';
import { writeToken } from './token-store';

/** 会话口令所在的请求头（与引擎 app/api/auth.py 的 LOCAL_SESSION_SECRET_HEADER 一致）。 */
export const SESSION_SECRET_HEADER = 'X-Polaris-Session-Secret';

/** POST /auth/local-session（无 body）。引擎连不上或拒绝时抛错。 */
export async function requestLocalSessionToken(): Promise<string> {
  const headers = new Headers();
  const secret = localSessionSecret();
  if (secret) headers.set(SESSION_SECRET_HEADER, secret);
  const res = await fetch(`${apiBase()}/auth/local-session`, { method: 'POST', headers });
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

/* —— 哪些 401 算「会话失效」 ——
   只有登录态本身无效时才该重取会话：fastapi-users 的 Bearer 校验失败回的是
   detail "Unauthorized"（没带/过期/签名不对都是它）；批量下载接口在既没有扩展 key、
   也没有有效会话时回 DOWNLOAD_AUTH_REQUIRED。其余 401 有自己的含义——比如设置页
   「测试浏览器扩展 key」填错了回 DOWNLOAD_API_KEY_INVALID——要原样报给用户，
   而不是把整页刷掉（以前任何 401 都会触发整页刷新）。 */
const SESSION_INVALID_DETAILS = new Set(['Unauthorized', 'DOWNLOAD_AUTH_REQUIRED']);

export function isSessionExpiry(
  status: number,
  detail: string | null | undefined,
  requestHeaders?: Headers,
): boolean {
  if (status !== 401) return false;
  // 带着扩展 key 的请求认的是 key 不是会话：key 不对与会话无关
  if (requestHeaders?.has('X-Polaris-API-Key')) return false;
  // 没有可读的 detail（非 JSON 响应）按会话失效处理：那只可能来自鉴权层本身
  if (detail == null || detail === '') return true;
  return SESSION_INVALID_DETAILS.has(detail);
}

/* —— 整页刷新的节流 ——
   重取会话后整页刷新，让所有查询带新会话重来。但如果新会话也立刻被拒（比如引擎
   刚换了密钥又出了别的问题），无条件刷新会陷入「刷新→401→刷新」的死循环。
   sessionStorage 跨刷新保留：10 秒内最多刷新一次。 */
const RELOAD_GUARD_KEY = 'polaris.session-reload-at';
export const RELOAD_GUARD_MS = 10_000;

interface GuardStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function sessionStore(): GuardStorage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage;
  } catch {
    return null;
  }
}

/** 现在能不能刷新；能的话顺手记下这次刷新的时间。存储不可用时放行（与以前一致）。 */
export function claimReload(now: number = Date.now(), storage: GuardStorage | null = sessionStore()): boolean {
  if (!storage) return true;
  try {
    const last = Number(storage.getItem(RELOAD_GUARD_KEY) ?? 0);
    if (Number.isFinite(last) && last > 0 && now - last >= 0 && now - last < RELOAD_GUARD_MS) {
      return false;
    }
    storage.setItem(RELOAD_GUARD_KEY, String(now));
    return true;
  } catch {
    return true;
  }
}

/* —— 401 统一处理 ——
   api.ts 的请求封装撞到「会话失效」的 401 时调这里：重取会话，然后（节流允许的话）
   整页刷新。取不到（引擎没连上）就清掉失效的 token——刷新后路由守卫会再试一次，
   仍失败就显示带重试的兜底页。并发 401 只触发一次。 */
let recovering = false;

export function handleUnauthorized(): void {
  if (typeof window === 'undefined') return;
  if (recovering) return;
  recovering = true;
  void (async () => {
    const token = await acquireLocalSession();
    if (!token) writeToken(null);
    if (claimReload()) {
      window.location.reload();
      return;
    }
    // 10 秒内刚刷新过：不再刷新，让这次请求的错误照常显示；新会话（若取到）已落存储，
    // 之后的请求会带上它。放开闸门，下一次真正的失效还能再处理。
    recovering = false;
  })();
}
