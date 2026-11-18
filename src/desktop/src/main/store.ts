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
  window: WindowState;
  /**
   * 插件市场索引源（#708）。
   * @deprecated #737 起真相在 kernel 持久层（PluginMetaStore 'market:endpoint'，
   * 见 methods.market.ts）：这里只剩迁移期读穿回退与「持久层不可用」的降级落点，
   * 一期后随回退一起删。electron store 只放纯壳偏好（窗口状态）。
   */
  marketEndpoint: string;
  /** 联网设置：首启安装（下载 Python、装依赖）用的代理与镜像策略。见 network.ts。 */
  network: StoredNetwork;
}

export interface StoredNetwork {
  /** 代理地址；空串 = 不指定（沿用环境变量 / 系统代理）。 */
  proxy: string;
  /** auto = 官方源不通再换国内镜像；on = 先用镜像；off = 不用镜像。 */
  mirror: 'auto' | 'on' | 'off';
}

const DEFAULTS: DesktopConfig = {
  window: { width: 1440, height: 900, maximized: false },
  marketEndpoint: MARKET_ENDPOINT_DEFAULT,
  network: { proxy: '', mirror: 'auto' },
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
      window: { ...DEFAULTS.window, ...(raw.window ?? {}) },
      // 空串不保留：历史配置文件没有该键或被清空时一律回官方默认源
      marketEndpoint:
        typeof raw.marketEndpoint === 'string' && raw.marketEndpoint
          ? raw.marketEndpoint
          : DEFAULTS.marketEndpoint,
      network: {
        proxy: typeof raw.network?.proxy === 'string' ? raw.network.proxy : '',
        mirror: raw.network?.mirror === 'on' || raw.network?.mirror === 'off' ? raw.network.mirror : 'auto',
      },
    };
  } catch {
    // 首次启动或文件损坏：回默认值，不阻断启动
    cache = { ...DEFAULTS, window: { ...DEFAULTS.window }, network: { ...DEFAULTS.network } };
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
