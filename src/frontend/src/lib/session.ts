/* 桌面端会话：把登录令牌从明文 localStorage 挪进系统钥匙串。

   token-store 的接口是**同步**的（api.ts / sse.ts / ws.ts / yjs-provider.ts 都在
   同步路径上取 token），而钥匙串读写要过 IPC。做法是启动时异步读一次、之后用
   内存缓存同步应答，写入异步透传——接口形状不变，调用方一行不用改。

   钥匙串不可用时（Linux 无 keyring）不安装，保持 localStorage，行为与 web 一致。 */

import { getSecret, hasHost, secretsAvailable, setSecret } from './host';
import { setTokenStore } from './token-store';

const SECRET_KEY = 'auth.token';
const LEGACY_LOCALSTORAGE_KEY = 'polaris.token';

export async function installDesktopTokenStore(): Promise<void> {
  if (!hasHost()) return;
  if (!(await secretsAvailable())) return;

  let cached = await getSecret(SECRET_KEY);

  // 迁移：旧版本把令牌明文存在 localStorage，搬进钥匙串后清掉原件
  if (cached === null) {
    const legacy = localStorage.getItem(LEGACY_LOCALSTORAGE_KEY);
    if (legacy) {
      cached = legacy;
      await setSecret(SECRET_KEY, legacy);
      localStorage.removeItem(LEGACY_LOCALSTORAGE_KEY);
    }
  }

  setTokenStore({
    get: () => cached,
    set: (token) => {
      cached = token;
      void setSecret(SECRET_KEY, token);
    },
  });
}
