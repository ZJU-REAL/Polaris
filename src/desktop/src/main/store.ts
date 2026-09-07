/* 配置持久化：userData/config.json。

   刻意不引入 electron-store —— 我们只有两个键，而 electron-store 9+ 是纯 ESM，
   打进 CJS 主进程要额外处理互操作；锁 8.x 又是在给自己留一个过时依赖。
   这里四十行手写，原子写入（临时文件 + rename），没有第三方依赖。 */

import { app } from 'electron';
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

import { MARKET_ENDPOINT_DEFAULT } from '../shared/contract';

export interface WindowState {
  x?: number;
  y?: number;
  width: number;
  height: number;
  maximized: boolean;
}

export interface DesktopConfig {
  /** 空串 = 尚未配置，前端进入首启配置页。 */
  serverUrl: string;
  window: WindowState;
  /** 插件市场索引源（#708）。可换源是一等能力，故进持久配置而不是写死。 */
  marketEndpoint: string;
}

/** 内部分发时可用 POLARIS_DEFAULT_SERVER_URL 预填默认服务器，避免把内网地址写死进源码。 */
const DEFAULTS: DesktopConfig = {
  serverUrl: process.env.POLARIS_DEFAULT_SERVER_URL ?? '',
  window: { width: 1440, height: 900, maximized: false },
  marketEndpoint: MARKET_ENDPOINT_DEFAULT,
};

let cache: DesktopConfig | null = null;

function configPath(): string {
  return join(app.getPath('userData'), 'config.json');
}

export function readConfig(): DesktopConfig {
  if (cache) return cache;
  try {
    const raw = JSON.parse(readFileSync(configPath(), 'utf8')) as Partial<DesktopConfig>;
    cache = {
      serverUrl: typeof raw.serverUrl === 'string' ? raw.serverUrl : DEFAULTS.serverUrl,
      window: { ...DEFAULTS.window, ...(raw.window ?? {}) },
      // 空串不保留：历史配置文件没有该键或被清空时一律回官方默认源
      marketEndpoint:
        typeof raw.marketEndpoint === 'string' && raw.marketEndpoint
          ? raw.marketEndpoint
          : DEFAULTS.marketEndpoint,
    };
  } catch {
    // 首次启动或文件损坏：回默认值，不阻断启动
    cache = { ...DEFAULTS, window: { ...DEFAULTS.window } };
  }
  return cache;
}

export function writeConfig(patch: Partial<DesktopConfig>): DesktopConfig {
  const next = { ...readConfig(), ...patch };
  cache = next;
  const target = configPath();
  try {
    mkdirSync(dirname(target), { recursive: true });
    const tmp = `${target}.tmp`;
    writeFileSync(tmp, JSON.stringify(next, null, 2), 'utf8');
    renameSync(tmp, target);
  } catch (err) {
    console.error('[polaris] 配置写入失败', err);
  }
  return next;
}
