/* ============================================================
   plugins.market.* 的实现（#708）：市场索引 + 安装/卸载 + 源配置。

   语义全部住在 kernel（@polaris/kernel 的 market/）里，这里只负责：
   - 能力门槛：与 plugins.* 同一事实源（kernelConfigTree），install/
     uninstall 额外要求 pluginMeta 在位——安装记录进不了持久层，装载前
     哈希复核链路就是断的，宁可拒装也不装出「无记录安装物」；
   - install 的长任务化：invoke 立刻返回 JobHandle，kernel 的四个安装
     阶段（下载/校验/解压/登记）翻译成 job.progress，成败走 job.done/
     job.error（错误码用 MarketError.code，前端可分流展示）；
   - 索引源持久化（#737 配置分层）：市场源配置的是 kernel 的行为（索引
     从哪拉、装什么包），归内核持久层——存 PluginMetaStore（kernel 的
     SQLite KV）键 'market:endpoint'。旧 electron store 的 marketEndpoint
     只作读穿回退：KV 无值时读旧 store，非默认值顺手迁移写入 KV（一期
     后删）。KV 不可用的降级会话（storage 挂载失败）退回旧 store，壳的
     换源能力不因持久层故障消失。空串复位官方默认。

   fetch 经模块级变量注入：smoke 测试把它换成离线替身跑完整安装链路，
   生产路径永远是 globalThis.fetch（不换源不叠代理，网络语义与索引
   拉取保持一致）。
   ============================================================ */

import {
  MarketError,
  fetchIndex,
  installAndRegister,
  uninstallAndRemove,
  type FetchImpl,
  type SqliteTree,
} from '@polaris/kernel';

import {
  ERR_CAPABILITY_UNAVAILABLE,
  ERR_INVALID_PARAMS,
  MARKET_ENDPOINT_DEFAULT,
  type JobHandle,
  type MarketEndpoint,
  type MarketIndexEntry,
  type MarketUninstallResult,
} from '../../shared/contract';
import { kernelConfigTree, kernelPluginMeta, marketPluginsDir } from '../kernel';
import { readConfig, writeConfig } from '../store';
import { fail, finish, progress, startJob } from './events';

/** 市场索引源在 kernel 持久层（PluginMetaStore）里的键（#737）。 */
export const MARKET_ENDPOINT_META_KEY = 'market:endpoint';

/** 当前生效的索引源：优先 kernel KV，读穿旧 electron store（见文件头）。 */
function readEndpoint(): string {
  const meta = kernelPluginMeta();
  const stored = meta?.get(MARKET_ENDPOINT_META_KEY);
  if (typeof stored === 'string' && stored) return stored;
  const legacy = readConfig().marketEndpoint;
  // 迁移写入只搬「用户改过源」这个事实；默认值不落 KV，让「从未配置」
  // 与「显式选了官方源」保持可区分（也避免每次读都白写一行）。
  if (meta && legacy !== MARKET_ENDPOINT_DEFAULT) meta.set(MARKET_ENDPOINT_META_KEY, legacy);
  return legacy;
}

function writeEndpoint(endpoint: string): void {
  const meta = kernelPluginMeta();
  if (meta) {
    meta.set(MARKET_ENDPOINT_META_KEY, endpoint);
    return;
  }
  // 持久层不可用（内存树会话）：退回旧 store，至少本机仍能换源
  writeConfig({ marketEndpoint: endpoint });
}

/** 安装阶段 → job.progress 的进度分子（分母恒为 4）。 */
const PHASE_STEP: Record<string, number> = { download: 1, verify: 2, extract: 3, register: 4 };

let fetchImpl: FetchImpl | undefined;

/** 仅供 smoke：注入离线 fetch 替身；传 undefined 恢复真实网络。 */
export function setMarketFetchForTesting(impl: FetchImpl | undefined): void {
  fetchImpl = impl;
}

/** 尚未落定的安装任务，仅供 smoke 等待 job 收尾（生产走 job.* 事件）。 */
const pendingInstalls = new Map<string, Promise<void>>();

export function awaitInstallForTesting(jobId: string): Promise<void> {
  return pendingInstalls.get(jobId) ?? Promise.resolve();
}

function requireMarket(): { tree: SqliteTree; meta: NonNullable<ReturnType<typeof kernelPluginMeta>> } {
  const tree = kernelConfigTree();
  if (!tree) {
    throw new Error(`${ERR_CAPABILITY_UNAVAILABLE}: plugins.manage — kernel 配置树不可用`);
  }
  const meta = kernelPluginMeta();
  if (!meta) {
    // storage 挂载失败的内存树会话：没有持久层就没有安装记录，
    // 哈希复核无从谈起，市场写操作整体不可用
    throw new Error(`${ERR_CAPABILITY_UNAVAILABLE}: plugins.market — 持久层不可用，无法记录安装`);
  }
  return { tree, meta };
}

export async function marketFetchIndex(): Promise<MarketIndexEntry[]> {
  // 读索引不需要树/持久层，网络与校验失败原样抛给前端展示
  return await fetchIndex(readEndpoint(), { fetchImpl });
}

export function marketInstall(name: string, version: string): JobHandle {
  const { tree, meta } = requireMarket(); // 门槛在返回 JobHandle 之前查：装不了就立刻报错
  const jobId = startJob('plugins.market.install');
  const run = (async () => {
    try {
      const { entryId, record } = await installAndRegister({
        name,
        version,
        pluginsDir: marketPluginsDir(),
        fetchImpl,
        tree,
        metaStore: meta,
        onPhase: (phase) => progress(jobId, phase, PHASE_STEP[phase] ?? 0, 4),
      });
      finish(jobId, { entryId, name: record.name, version: record.version });
    } catch (err) {
      // MarketError.code 透传给前端分流（integrity-mismatch / registry-http…）
      const code = err instanceof MarketError ? err.code : 'install-failed';
      fail(jobId, code, err instanceof Error ? err.message : String(err));
    } finally {
      pendingInstalls.delete(jobId);
    }
  })();
  pendingInstalls.set(jobId, run);
  return { jobId };
}

export async function marketUninstall(name: string): Promise<MarketUninstallResult> {
  const { tree, meta } = requireMarket();
  return await uninstallAndRemove({
    name,
    pluginsDir: marketPluginsDir(),
    tree,
    metaStore: meta,
  });
}

export function marketGetEndpoint(): MarketEndpoint {
  const endpoint = readEndpoint();
  return { endpoint, isDefault: endpoint === MARKET_ENDPOINT_DEFAULT };
}

export function marketSetEndpoint(endpoint: string): MarketEndpoint {
  // 空串 = 复位默认源（UI 的「恢复官方源」不需要知道默认值是什么）
  const next = endpoint === '' ? MARKET_ENDPOINT_DEFAULT : endpoint;
  let parsed: URL;
  try {
    parsed = new URL(next);
  } catch {
    throw new Error(`${ERR_INVALID_PARAMS}: endpoint must be a valid URL`);
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new Error(`${ERR_INVALID_PARAMS}: endpoint must be http(s)`);
  }
  writeEndpoint(next);
  return { endpoint: next, isDefault: next === MARKET_ENDPOINT_DEFAULT };
}
