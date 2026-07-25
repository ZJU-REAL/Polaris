/* ============================================================
   凭据存储：Electron safeStorage（macOS Keychain / Windows DPAPI /
   Linux libsecret）+ userData 下的密文文件。

   桌面端把登录会话放这里而不是 renderer 的 localStorage：localStorage 是明文
   落盘的，任何能读到用户目录的进程都能拿走会话。

   Linux 上没有可用 keyring 时 safeStorage 会退化成弱加密（basic_text）。
   那种情况下**拒绝存储**而不是静默降级——前端会回落到 localStorage，
   行为与 web 一致，至少不会让人误以为凭据被系统保护着。

   这里只存会话令牌，不存密码：密码泄露是全账号失守且无法吊销，而会话可以
   吊销、有作用域、会过期。
   ============================================================ */

import { app, safeStorage } from 'electron';
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

/** 允许存储的键白名单：renderer 传来的键名一律不可信。 */
const ALLOWED_KEYS = new Set(['auth.token']);

type Vault = Record<string, string>; // key -> base64(密文)

let cache: Vault | null = null;

function vaultPath(): string {
  return join(app.getPath('userData'), 'secrets.json');
}

function readVault(): Vault {
  if (cache) return cache;
  try {
    cache = JSON.parse(readFileSync(vaultPath(), 'utf8')) as Vault;
  } catch {
    cache = {};
  }
  return cache;
}

function writeVault(vault: Vault): void {
  cache = vault;
  const target = vaultPath();
  try {
    mkdirSync(dirname(target), { recursive: true });
    const tmp = `${target}.tmp`;
    writeFileSync(tmp, JSON.stringify(vault), { encoding: 'utf8', mode: 0o600 });
    renameSync(tmp, target);
  } catch (err) {
    console.error('[polaris] 凭据写入失败', err);
  }
}

export function secretsAvailable(): boolean {
  try {
    return safeStorage.isEncryptionAvailable();
  } catch {
    return false;
  }
}

export function getSecret(key: string): string | null {
  if (!ALLOWED_KEYS.has(key) || !secretsAvailable()) return null;
  const blob = readVault()[key];
  if (!blob) return null;
  try {
    return safeStorage.decryptString(Buffer.from(blob, 'base64'));
  } catch {
    // 密钥轮换 / 换机器后解不开：当作没有，让用户重新登录
    return null;
  }
}

export function setSecret(key: string, value: string | null): boolean {
  if (!ALLOWED_KEYS.has(key)) return false;
  if (!secretsAvailable()) return false;
  const vault = { ...readVault() };
  if (value === null) {
    delete vault[key];
  } else {
    vault[key] = safeStorage.encryptString(value).toString('base64');
  }
  writeVault(vault);
  return true;
}
