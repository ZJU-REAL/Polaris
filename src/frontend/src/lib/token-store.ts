/* ============================================================
   会话 token 的存储后端。

   token 只来自本地会话（见 local-session.ts），落 localStorage（桌面端页面跑在
   app://polaris 这个稳定 origin 上，localStorage 正常持久化）。丢了也无妨：
   下次请求撞 401 会自动再取一个。

   注意：**不要改用 Electron 的 safeStorage**。试过并退回了——未签名/ad-hoc
   签名的包每次构建签名都不同，钥匙串 ACL 对不上，macOS 每次启动都弹授权框。

   注意：接口刻意保持**同步**。改成 async 会波及 request/requestBlob
   与三个 WS/SSE 调用点，那才是真正的重写。
   ============================================================ */

const TOKEN_KEY = 'polaris.token';
/** 以前登录页「记住密码」留下的键，账号层删掉后（#842）顺手清掉。 */
const LEGACY_KEYS = ['polaris.remember', 'polaris.last-account'];

function safe<T>(fn: () => T, fallback: T): T {
  // 隐私模式 / 禁用存储的浏览器里访问 storage 会抛异常
  try {
    return fn();
  } catch {
    return fallback;
  }
}

export function readToken(): string | null {
  return safe(() => localStorage.getItem(TOKEN_KEY), null);
}

export function writeToken(token: string | null): void {
  safe(() => {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
    // 旧版本「不记住密码」时 token 落在 sessionStorage
    sessionStorage.removeItem(TOKEN_KEY);
    for (const key of LEGACY_KEYS) localStorage.removeItem(key);
    return null;
  }, null);
}
