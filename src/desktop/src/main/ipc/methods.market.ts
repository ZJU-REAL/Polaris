/* ============================================================
   plugins.market.* 在桌面侧的绑定（#708，#754 起实现搬进 kernel）。

   语义与守卫都住在 @polaris/kernel 的 rpc/market-methods.ts，服务器形态用
   的是同一份。这里只接三样桌面独有的东西：

   - 宿主句柄（树 / 持久层 / 安装物目录）——都是模块级单例，传函数现读；
   - job 事件的落点——桌面经 webContents 推给渲染进程（events.ts）；
   - 旧 electron store 的索引源读穿——一次性迁移，服务器没有这回事，
     所以在 kernel 那边是可选依赖。

   fetch 替身仍由本模块持有：冒烟把它换成离线实现跑完整安装链路，
   生产路径永远是 globalThis.fetch。
   ============================================================ */

import {
  MARKET_ENDPOINT_META_KEY as KERNEL_MARKET_ENDPOINT_META_KEY,
  OFFICIAL_INDEX_URL,
  createMarketMethods,
  type FetchImpl,
  type RpcMethod,
} from '@polaris/kernel';

import {
  MARKET_ENDPOINT_DEFAULT,
  type MarketEndpoint,
  type MarketIndexEntry,
  type MarketUninstallResult,
  type MethodName,
} from '../../shared/contract';
import { kernelConfigTree, kernelPluginMeta, marketPluginsDir } from '../kernel';
import { readConfig, writeConfig } from '../store';
import { jobBus } from './events';

let fetchImpl: FetchImpl | undefined;

/** 仅供 smoke：注入离线 fetch 替身；传 undefined 恢复真实网络。 */
export function setMarketFetchForTesting(impl: FetchImpl | undefined): void {
  fetchImpl = impl;
}

const market = createMarketMethods({
  configTree: () => kernelConfigTree(),
  pluginMeta: () => kernelPluginMeta(),
  pluginsDir: () => marketPluginsDir(),
  jobs: jobBus,
  fetchImpl: () => fetchImpl,
  legacyEndpoint: {
    read: () => readConfig().marketEndpoint,
    write: (endpoint) => writeConfig({ marketEndpoint: endpoint }),
  },
});

type MarketMethodName = Extract<MethodName, `plugins.market.${string}`>;

export const marketMethods = market.methods as Record<MarketMethodName, RpcMethod>;

/** 尚未落定的安装任务，仅供 smoke 等待 job 收尾（生产走 job.* 事件）。 */
export function awaitInstallForTesting(jobId: string): Promise<void> {
  return market.awaitInstall(jobId);
}

/* 具名直调入口：冒烟在主进程内不经 IPC 驱动这几个方法。 */

export function marketFetchIndex(): Promise<MarketIndexEntry[]> {
  return marketMethods['plugins.market.fetchIndex'](undefined) as Promise<MarketIndexEntry[]>;
}

export function marketGetEndpoint(): MarketEndpoint {
  return marketMethods['plugins.market.getEndpoint'](undefined) as MarketEndpoint;
}

export function marketSetEndpoint(endpoint: string): MarketEndpoint {
  return marketMethods['plugins.market.setEndpoint']({ endpoint }) as MarketEndpoint;
}

// 默认索引源的字面量在两处各有一份：kernel 的 OFFICIAL_INDEX_URL（isDefault
// 判定用它）与渲染层契约的 MARKET_ENDPOINT_DEFAULT。两边漂了的话，「恢复官方源」
// 会写进一个前端永远认不出是默认值的地址——安静地坏掉。启动即对账，宁可炸在这里。
if (MARKET_ENDPOINT_DEFAULT !== OFFICIAL_INDEX_URL) {
  throw new Error(
    `market endpoint default drifted: contract=${MARKET_ENDPOINT_DEFAULT} kernel=${OFFICIAL_INDEX_URL}`,
  );
}

/** 市场索引源在持久层里的键；冒烟直接查这一行，从 kernel 转出保持单一来源。 */
export const MARKET_ENDPOINT_META_KEY = KERNEL_MARKET_ENDPOINT_META_KEY;

export function marketInstall(name: string, version: string): { jobId: string } {
  return marketMethods['plugins.market.install']({ name, version }) as { jobId: string };
}

export function marketUninstall(name: string): Promise<MarketUninstallResult> {
  return marketMethods['plugins.market.uninstall']({ name }) as Promise<MarketUninstallResult>;
}
