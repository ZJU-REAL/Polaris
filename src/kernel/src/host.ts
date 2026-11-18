/* ============================================================
   插件宿主装配（#754）：「持久层 → 配置树 → loader → fiber」这套开机
   顺序。装哪些内置插件、三方包的 import 基准在哪、哈希复核在装载前还是
   装载后，都在这一处定。

   本模块刻意不认识 electron：数据根与严格模式由调用方给，桌面传
   app.getPath('userData') 与 app.isPackaged，测试传临时目录。
   （src/kernel 全包都由 tests/electron-free.test.ts 机械把关。）

   种子树是参数，不是常量：桌面还要按 env/打包引导注入 legacy-engine 的
   运行参数，这些留在调用方。
   ============================================================ */

import { copyFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

import { Loader } from '@deepseek-ai/cordis-plugin-loader'

import { MemoryConfigTreeStore, type ConfigEntry, type ConfigTreeStore } from './config/tree.ts'
import { SqliteTree } from './config/sqlite-tree.ts'
import { createKernel, type Kernel } from './kernel.ts'
import { createImportGuard, verifyInstalledEntries, type InstallVerifyIssue } from './market/lifecycle.ts'
import { BUILTIN_PLUGINS, GROUP_BUILTIN, registerBuiltins } from './plugins/builtins.ts'
import { legacyEngine, type LegacyEngineConfig } from './plugins/legacy-engine.ts'
import { storage, type StorageService } from './plugins/storage.ts'
import type { PluginMetaStore } from './storage/store.ts'

/** 配置树备份在 PluginMetaStore 里的键前缀（后面跟 <原因>:<时间戳>）。 */
export const CONFIG_TREE_BACKUP_PREFIX = 'config-tree:backup:'

/**
 * 启动时对存下来的配置树动过的手脚。两种：
 * - dropped-builtins：清掉了这个版本已经没有的内置条目（旧版本种的
 *   legacy-engine / sources / desktop-probe），其余条目原样保留；
 * - reset：存下来的树装不起来，备份后重置成种子树。
 */
export interface TreeRepair {
  kind: 'dropped-builtins' | 'reset'
  /** dropped-builtins：被清掉的条目 id。 */
  dropped?: string[]
  /** reset：当初装不起来的原因。 */
  reason?: string
  /** 备份在 PluginMetaStore 里的键（持久层不可用时没有）。 */
  backupKey?: string
  /** 备份文件的绝对路径（写不出来时没有）。 */
  backupFile?: string
}

/**
 * 去掉名字是 `cordis:<键>` 但 <键> 不在 known 里的条目（递归进组）。
 *
 * 为什么必须在装载前做：树装载是事务化的，一条未知内置条目就让整棵树
 * 装不起来（SqliteTree.import 直接抛错）。旧版本把 legacy-engine / sources /
 * desktop-probe 种进了树，这些内置插件现在都不在表里了。
 * 非 cordis: 的条目（用户装的三方插件）一律不碰。
 */
export function dropUnknownBuiltins(
  entries: ConfigEntry[],
  known: ReadonlySet<string>,
): { entries: ConfigEntry[]; dropped: string[] } {
  const dropped: string[] = []
  const walk = (list: ConfigEntry[]): ConfigEntry[] => {
    const kept: ConfigEntry[] = []
    for (const entry of list) {
      const name = typeof entry?.name === 'string' ? entry.name : ''
      if (name.startsWith('cordis:') && !known.has(name.slice(7))) {
        dropped.push(String(entry.id))
        continue
      }
      kept.push(Array.isArray(entry.children) ? { ...entry, children: walk(entry.children) } : entry)
    }
    return kept
  }
  return { entries: walk(entries), dropped }
}

/**
 * 把本地引擎直接挂到内核上（不进配置树，理由见 plugins/builtins.ts）。
 * resolve 时引擎已通过健康检查、`ctx.get('legacy')` 可用；起不来则抛错，
 * 子进程已由插件自己的 disposer 回收。停机时随 kernel.stop() 级联回收。
 */
export async function mountEngine(kernel: Kernel, config: LegacyEngineConfig): Promise<void> {
  await kernel.ctx.plugin(legacyEngine, config)
}

export interface PluginHostLog {
  warn(message: string): void
  error(message: string, error?: unknown): void
}

export interface CreatePluginHostOptions {
  /** 内核实例名（出现在 kernel.status / 日志里）。 */
  name: string
  /** 数据根：storage.db 落在 <dataRoot>/kernel/，市场安装物落在 <dataRoot>/plugins/。 */
  dataRoot: string
  /**
   * 首启种子树。**只在 store 完全为空时写入**——树是用户状态的真相，
   * 任何非空内容都不做「补齐」，那会把用户显式删掉的条目悄悄种回来。
   */
  seedEntries: ConfigEntry[]
  /**
   * 安装物哈希复核的严格模式。true = 对不上就强制禁用（打包态/生产），
   * false = 仅告警（开发态本地改插件入口文件是正常动作）。
   */
  strict: boolean
  log?: PluginHostLog
}

export interface PluginHost {
  kernel: Kernel
  /** 装载失败时为 undefined：宿主仍然可用，只是这次会话没有插件树。 */
  configTree?: SqliteTree
  store: ConfigTreeStore
  /** storage 挂载失败时为 undefined（此时 store 是内存实现，改动不落盘）。 */
  pluginMeta?: PluginMetaStore
  installIssues: InstallVerifyIssue[]
  /** 市场安装物根目录，调用方要用它做 ctx.baseUrl 之外的路径推导时可取。 */
  pluginsDir: string
  /** 启动时对存下来的树做过的修复（没有则为空数组），宿主据此记日志。 */
  repairs: TreeRepair[]
}

/** 市场安装物的落盘根：<dataRoot>/plugins/<包名>/<版本>/。 */
export function marketPluginsDir(dataRoot: string): string {
  return join(dataRoot, 'plugins')
}

const consoleLog: PluginHostLog = {
  warn: (message) => console.warn(message),
  error: (message, error) => console.error(message, error),
}

/**
 * 按固定顺序装配插件宿主。顺序本身是契约，别重排：
 *
 * 1. storage 先于 loader 直挂——配置树就存在它里面；失败则降级为内存 store，
 *    宿主照常起（kernel.status 的 storage=false 把这件事暴露出去）。
 * 2. ctx.baseUrl 必须在 Loader/SqliteTree 之前设好：vendor loader 用
 *    new URL(name, ctx.baseUrl) 解析相对 specifier，EntryTree 构造时从父 ctx
 *    拷贝，晚设就来不及了。
 * 3. 哈希复核在**树装载之前**：树装载是事务化 reconcile，单条目 import 抛错
 *    会整树回滚；只有装载前改 store 才能做到「坏的禁掉、其余照常」。
 */
export async function createPluginHost(options: CreatePluginHostOptions): Promise<PluginHost> {
  const log = options.log ?? consoleLog
  const pluginsDir = marketPluginsDir(options.dataRoot)
  const kernel = createKernel({ name: options.name })

  try {
    await kernel.ctx.plugin(storage, { path: join(options.dataRoot, 'kernel', 'storage.db') })
  } catch (err) {
    log.error('[kernel] storage 持久层挂载失败，本次会话不落盘：', err)
  }

  const storageSvc = kernel.ctx.get('storage') as StorageService | undefined
  const store: ConfigTreeStore = storageSvc?.configTree ?? new MemoryConfigTreeStore()
  const pluginMeta = storageSvc?.pluginMeta

  // 尾部斜杠是 URL 基准目录的规矩：少了最后一段会被当文件名换掉
  kernel.ctx.baseUrl = `${pathToFileURL(pluginsDir).href}/`

  let installIssues: InstallVerifyIssue[] = []
  if (pluginMeta) {
    try {
      installIssues = await verifyInstalledEntries({ metaStore: pluginMeta, store, strict: options.strict })
      for (const issue of installIssues) log.warn(`[kernel] ${issue.message}`)
    } catch (err) {
      log.error('[kernel] 安装记录哈希复核失败（跳过，不阻断启动）：', err)
    }
  }

  const repairs: TreeRepair[] = []
  const backupDir = join(options.dataRoot, 'kernel', 'backups')
  const stamp = (): string => new Date().toISOString().replace(/[:.]/g, '-')

  /** 改树之前留底：KV 一份（跟着 storage.db 走）、文件一份（用户/支持人员找得到）。 */
  const backupTree = (
    label: string,
    entries: ConfigEntry[],
    reason: string,
  ): Pick<TreeRepair, 'backupKey' | 'backupFile'> => {
    const at = stamp()
    const out: Pick<TreeRepair, 'backupKey' | 'backupFile'> = {}
    const payload = { reason, savedAt: new Date().toISOString(), entries }
    try {
      const key = `${CONFIG_TREE_BACKUP_PREFIX}${label}:${at}`
      pluginMeta?.set(key, payload)
      if (pluginMeta) out.backupKey = key
    } catch (err) {
      log.error('[kernel] 配置树备份写入持久层失败：', err)
    }
    try {
      mkdirSync(backupDir, { recursive: true })
      const file = join(backupDir, `config-tree-${label}-${at}.json`)
      writeFileSync(file, `${JSON.stringify(payload, null, 2)}\n`)
      out.backupFile = file
    } catch (err) {
      log.error('[kernel] 配置树备份文件写入失败：', err)
    }
    return out
  }

  const mountTree = async (): Promise<SqliteTree | undefined> => {
    await kernel.ctx.plugin(SqliteTree, {
      store,
      // 第二道闸：会话运行中被篡改、用户再点启用时在 import 阶段拒绝
      guardImport: pluginMeta
        ? createImportGuard({
            metaStore: pluginMeta,
            strict: options.strict,
            warn: (message) => log.warn(`[kernel] ${message}`),
          })
        : undefined,
    })
    return kernel.ctx.get('configTree') as SqliteTree | undefined
  }

  let configTree: SqliteTree | undefined
  let loaderReady = false
  try {
    const stored = await store.load()
    if (stored.length === 0) {
      if (options.seedEntries.length) await store.save(options.seedEntries)
    } else {
      // 旧版本种下的内置条目（含可能带着绝对路径泄进树的引擎配置）在这里清掉
      const known = new Set([...Object.keys(BUILTIN_PLUGINS), GROUP_BUILTIN])
      const cleaned = dropUnknownBuiltins(stored, known)
      if (cleaned.dropped.length) {
        const backup = backupTree('dropped-builtins', stored, 'entries naming built-ins this version no longer has')
        await store.save(cleaned.entries)
        repairs.push({ kind: 'dropped-builtins', dropped: cleaned.dropped, ...backup })
        log.warn(`[kernel] 配置树清掉了已不存在的内置条目：${cleaned.dropped.join(', ')}`)
      }
    }
    await kernel.ctx.plugin(Loader)
    registerBuiltins(kernel.ctx.loader)
    loaderReady = true
    configTree = await mountTree()
  } catch (err) {
    log.error('[kernel] 配置树装载失败，尝试备份后重置：', err)
    // 修复：存下来的树装不起来（条目损坏、三方插件的入口没了、库里的行读不出来）。
    // 以前的结果是这次会话没有树、之后每次启动都一样，而用户没有任何界面能改它。
    // 现在把原样备份下来，重置成种子树再装一次。
    const reason = err instanceof Error ? err.message : String(err)
    try {
      let broken: ConfigEntry[] = []
      let readable = true
      try {
        broken = await store.load()
      } catch {
        readable = false
      }
      const backup = backupTree('unloadable', broken, reason)
      if (!readable) {
        // 连读都读不出来：条目备份是空的，把整个库文件另存一份
        try {
          mkdirSync(backupDir, { recursive: true })
          copyFileSync(
            join(options.dataRoot, 'kernel', 'storage.db'),
            join(backupDir, `storage-unreadable-${stamp()}.db`),
          )
        } catch {
          /* 内存 store（storage 没挂上）时没有文件可拷 */
        }
      }
      await store.save(options.seedEntries)
      if (!loaderReady) {
        await kernel.ctx.plugin(Loader)
        registerBuiltins(kernel.ctx.loader)
        loaderReady = true
      }
      configTree = await mountTree()
      repairs.push({ kind: 'reset', reason, ...backup })
      log.warn(
        `[kernel] 配置树已重置为默认；原内容备份在 ${backup.backupFile ?? backup.backupKey ?? '（备份失败）'}`,
      )
    } catch (repairErr) {
      configTree = undefined
      log.error('[kernel] 配置树重置后仍装不起来，本次会话无插件树：', repairErr)
    }
  }
  // 扫描结论挂到条目注记上：被强制禁用的条目要在插件列表里说清原因
  for (const issue of installIssues) configTree?.warnings.set(issue.entryId, issue.message)

  return { kernel, configTree, store, pluginMeta, installIssues, pluginsDir, repairs }
}
