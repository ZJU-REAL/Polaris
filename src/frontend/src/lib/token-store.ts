/* ============================================================
   登录 token 的存储后端 —— 可替换。

   web 端与桌面端都用浏览器存储（桌面端页面跑在 app://polaris 这个稳定
   origin 上，localStorage 正常持久化，登录态零改动）。

   「记住密码」勾选与否决定 token 落在哪儿：
     勾选   → localStorage，关掉浏览器再回来仍是登录态
     不勾选 → sessionStorage，标签页关掉即失效
   读取时两处都看，所以切换偏好不会把已登录的人踢下线。

   注意：**不要改用 Electron 的 safeStorage**。试过并退回了——未签名/ad-hoc
   签名的包每次构建签名都不同，钥匙串 ACL 对不上，macOS 每次启动都弹授权框。
   要用钥匙串的前提是先有稳定的 Developer ID 签名。抽象本身保留：真到那一天，
   启动时 setTokenStore() 换个实现即可，调用方一行不改。

   注意：接口刻意保持**同步**。改成 async 会波及 request/requestBlob
   与三个 WS/SSE 调用点，那才是真正的重写。
   ============================================================ */

export interface TokenStore {
  get(): string | null;
  set(token: string | null): void;
}

const TOKEN_KEY = 'polaris.token';
const REMEMBER_KEY = 'polaris.remember';
/** 上次登录的账号，勾了「记住密码」时回填到登录框（只记账号，密码交给浏览器密码管理器）。 */
const LAST_ACCOUNT_KEY = 'polaris.last-account';

function safe<T>(fn: () => T, fallback: T): T {
  // 隐私模式 / 禁用存储的浏览器里访问 storage 会抛异常
  try {
    return fn();
  } catch {
    return fallback;
  }
}

export function isRemembered(): boolean {
  return safe(() => localStorage.getItem(REMEMBER_KEY) !== '0', true);
}

/** 切换「记住密码」，并把已有 token 迁到对应存储里。 */
export function setRemembered(remember: boolean): void {
  safe(() => {
    localStorage.setItem(REMEMBER_KEY, remember ? '1' : '0');
    const token = safe(
      () => sessionStorage.getItem(TOKEN_KEY) ?? localStorage.getItem(TOKEN_KEY),
      null,
    );
    localStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(TOKEN_KEY);
    if (token) (remember ? localStorage : sessionStorage).setItem(TOKEN_KEY, token);
    return null;
  }, null);
}

export function getLastAccount(): string {
  return safe(() => localStorage.getItem(LAST_ACCOUNT_KEY) ?? '', '');
}

export function setLastAccount(account: string | null): void {
  safe(() => {
    if (account) localStorage.setItem(LAST_ACCOUNT_KEY, account);
    else localStorage.removeItem(LAST_ACCOUNT_KEY);
    return null;
  }, null);
}

const browserTokenStore: TokenStore = {
  get: () =>
    safe(() => sessionStorage.getItem(TOKEN_KEY) ?? localStorage.getItem(TOKEN_KEY), null),
  set: (token) =>
    safe(() => {
      localStorage.removeItem(TOKEN_KEY);
      sessionStorage.removeItem(TOKEN_KEY);
      if (token) {
        (isRemembered() ? localStorage : sessionStorage).setItem(TOKEN_KEY, token);
      }
      return null;
    }, null),
};

let store: TokenStore = browserTokenStore;

/** 替换 token 存储后端（桌面端二期用）。 */
export function setTokenStore(next: TokenStore): void {
  store = next;
}

export function readToken(): string | null {
  return store.get();
}

export function writeToken(token: string | null): void {
  store.set(token);
}
