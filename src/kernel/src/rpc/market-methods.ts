/* ============================================================
   plugins.market.* 的实现，与传输无关（#754）。

   语义住在 market/ 下（索引校验、安装引擎、卸载），这里只负责三件事：
   - 能力门槛：树要在，**持久层也要在**——安装记录进不了持久层，装载前的
     哈希复核链路就是断的，宁可拒装也不装出「无记录安装物」；
   - install 的长任务化：立刻返回 JobHandle，四个安装阶段翻成 job.progress，
     成败走 job.done / job.error（错误码用 MarketError.code，前端可分流）；
   - 索引源读写：优先内核持久层的 KV，旧存储只作读穿回退。

   旧存储读穿（legacyEndpoint）是桌面独有的一次性迁移，服务器形态没有它，
   所以做成可选依赖而不是必需参数。
   ============================================================ */

import type { SqliteTree } from '../config/sqlite-tree.ts'
import { MarketError } from '../market/contract.ts'
import { OFFICIAL_INDEX_URL, fetchIndex } from '../market/index-client.ts'
import { installAndRegister, uninstallAndRemove } from '../market/lifecycle.ts'
import type { FetchImpl } from '../sources/contract.ts'
import type { PluginMetaStore } from '../storage/store.ts'
import type { JobBus } from './jobs.ts'
import {
  ERR_CAPABILITY_UNAVAILABLE,
  ERR_INVALID_PARAMS,
  asString,
  type RpcMethod,
} from './plugin-methods.ts'

/** 市场索引源在持久层（PluginMetaStore）里的键。 */
export const MARKET_ENDPOINT_META_KEY = 'market:endpoint'

/** 安装阶段 → job.progress 的进度分子（分母恒为 4）。 */
const PHASE_STEP: Record<string, number> = { download: 1, verify: 2, extract: 3, register: 4 }

export interface MarketEndpointInfo {
  endpoint: string
  isDefault: boolean
}

export interface MarketRpcDeps {
  configTree(): SqliteTree | null
  pluginMeta(): PluginMetaStore | null
  /** 市场安装物的落盘根。 */
  pluginsDir(): string
  jobs: JobBus
  /** 测试注入的离线 fetch 替身；生产返回 undefined 走真实网络。 */
  fetchImpl?(): FetchImpl | undefined
  /**
   * 旧存储读穿（桌面一次性迁移）。KV 里没有值时读它，且只在「用户改过源」
   * 时回写 KV——默认值不落 KV，让「从未配置」与「显式选了官方源」保持可区分。
   */
  legacyEndpoint?: { read(): string; write(endpoint: string): void }
}

export interface MarketMethods {
  methods: Record<string, RpcMethod>
  /** 仅供测试等待安装 job 收尾；生产路径走 job.* 事件。 */
  awaitInstall(jobId: string): Promise<void>
}

export function createMarketMethods(deps: MarketRpcDeps): MarketMethods {
  const pendingInstalls = new Map<string, Promise<void>>()
  const fetchImpl = () => deps.fetchImpl?.()

  function readEndpoint(): string {
    const meta = deps.pluginMeta()
    const stored = meta?.get(MARKET_ENDPOINT_META_KEY)
    if (typeof stored === 'string' && stored) return stored
    const legacy = deps.legacyEndpoint?.read() ?? OFFICIAL_INDEX_URL
    if (meta && legacy !== OFFICIAL_INDEX_URL) meta.set(MARKET_ENDPOINT_META_KEY, legacy)
    return legacy
  }

  function writeEndpoint(endpoint: string): void {
    const meta = deps.pluginMeta()
    if (meta) {
      meta.set(MARKET_ENDPOINT_META_KEY, endpoint)
      return
    }
    // 持久层不可用（内存树会话）：退回旧存储，至少本机仍能换源
    deps.legacyEndpoint?.write(endpoint)
  }

  function requireMarket(): { tree: SqliteTree; meta: PluginMetaStore } {
    const tree = deps.configTree()
    if (!tree) {
      throw new Error(`${ERR_CAPABILITY_UNAVAILABLE}: plugins.manage — kernel 配置树不可用`)
    }
    const meta = deps.pluginMeta()
    if (!meta) {
      throw new Error(`${ERR_CAPABILITY_UNAVAILABLE}: plugins.market — 持久层不可用，无法记录安装`)
    }
    return { tree, meta }
  }

  function endpointInfo(endpoint: string): MarketEndpointInfo {
    return { endpoint, isDefault: endpoint === OFFICIAL_INDEX_URL }
  }

  return {
    awaitInstall: (jobId) => pendingInstalls.get(jobId) ?? Promise.resolve(),
    methods: {
      // 读索引不需要树/持久层，网络与校验失败原样抛给前端展示
      'plugins.market.fetchIndex': () => fetchIndex(readEndpoint(), { fetchImpl: fetchImpl() }),

      'plugins.market.install': (p) => {
        const name = asString(p, 'name')
        const version = asString(p, 'version')
        // 门槛在返回 JobHandle 之前查：装不了就立刻报错，别先给一个注定失败的 job
        const { tree, meta } = requireMarket()
        const jobId = deps.jobs.start('plugins.market.install')
        const run = (async () => {
          try {
            const { entryId, record } = await installAndRegister({
              name,
              version,
              pluginsDir: deps.pluginsDir(),
              fetchImpl: fetchImpl(),
              tree,
              metaStore: meta,
              onPhase: (phase) => deps.jobs.progress(jobId, phase, PHASE_STEP[phase] ?? 0, 4),
            })
            deps.jobs.finish(jobId, { entryId, name: record.name, version: record.version })
          } catch (err) {
            // MarketError.code 透传给前端分流（integrity-mismatch / registry-http…）
            const code = err instanceof MarketError ? err.code : 'install-failed'
            deps.jobs.fail(jobId, code, err instanceof Error ? err.message : String(err))
          } finally {
            pendingInstalls.delete(jobId)
          }
        })()
        pendingInstalls.set(jobId, run)
        return { jobId }
      },

      'plugins.market.uninstall': async (p) => {
        const { tree, meta } = requireMarket()
        return await uninstallAndRemove({
          name: asString(p, 'name'),
          pluginsDir: deps.pluginsDir(),
          tree,
          metaStore: meta,
        })
      },

      'plugins.market.getEndpoint': () => endpointInfo(readEndpoint()),

      'plugins.market.setEndpoint': (p) => {
        // 空串 = 复位默认源（UI 的「恢复官方源」不需要知道默认值是什么）
        const raw = asString(p, 'endpoint')
        const next = raw === '' ? OFFICIAL_INDEX_URL : raw
        let parsed: URL
        try {
          parsed = new URL(next)
        } catch {
          throw new Error(`${ERR_INVALID_PARAMS}: endpoint must be a valid URL`)
        }
        if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
          throw new Error(`${ERR_INVALID_PARAMS}: endpoint must be http(s)`)
        }
        writeEndpoint(next)
        return endpointInfo(next)
      },
    },
  }
}
