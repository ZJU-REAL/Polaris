/* ============================================================
   登录 token 的存储后端 —— 可替换。

   web 端与桌面端一期都用 localStorage（桌面端页面跑在 app://polaris
   这个稳定 origin 上，localStorage 正常持久化，登录态零改动）。
   二期若把 token 迁到 Electron 的 safeStorage（macOS Keychain /
   Windows DPAPI / Linux libsecret），只需在启动时 setTokenStore()
   换一个实现，api.ts / sse.ts / ws.ts / yjs-provider.ts 一行不改。

   注意：接口刻意保持**同步**。改成 async 会波及 request/requestBlob
   与三个 WS/SSE 调用点，那才是真正的重写。
   ============================================================ */

export interface TokenStore {
  get(): string | null;
  set(token: string | null): void;
}

const TOKEN_KEY = 'polaris.token';

const localStorageTokenStore: TokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token) => {
    if (token) {
      localStorage.setItem(TOKEN_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
  },
};

let store: TokenStore = localStorageTokenStore;

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
