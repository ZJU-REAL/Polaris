/* ============================================================
   插件宿主装配（#754）：把「持久层 → 配置树 → loader → fiber」这套
   开机顺序从桌面主进程里抽出来，桌面与服务器形态共用同一份。

   抽出来的理由不是省代码，是**防漂移**：装哪些内置插件、种子树长什么样、
   三方包的 import 基准在哪、哈希复核在装载前还是装载后——这些决定了
   「插件世界」的形状。两个形态各写一份，迟早会变成两套语义不同的插件系统，
   而市场里同一个包要在两边都能装。

   本模块刻意不认识 electron：数据根与严格模式由调用方给，桌面传
   app.getPath('userData') 与 app.isPackaged，服务器传数据目录与生产标志。
   （src/kernel 全包都由 tests/electron-free.test.ts 机械把关。）

   形态差异留在调用方，不进这里：
   - 桌面还要按 env/打包引导注入 legacy-engine 的运行参数；
   - 服务器的 Python 边缘是独立容器，树里根本没有 legacy-engine。
   所以种子树是参数，不是常量。
   ============================================================ */

import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

import { Loader } from '@deepseek-ai/cordis-plugin-loader'

import { MemoryConfigTreeStore, type ConfigEntry, type ConfigTreeStore } from './config/tree.ts'
import { SqliteTree } from './config/sqlite-tree.ts'
import { createKernel, type Kernel } from './kernel.ts'
import { createImportGuard, verifyInstalledEntries, type InstallVerifyIssue } from './market/lifecycle.ts'
import { registerBuiltins } from './plugins/builtins.ts'
import { storage, type StorageService } from './plugins/storage.ts'
import type { PluginMetaStore } from './storage/store.ts'

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

  let configTree: SqliteTree | undefined
  try {
    if ((await store.load()).length === 0) await store.save(options.seedEntries)
    await kernel.ctx.plugin(Loader)
    registerBuiltins(kernel.ctx.loader)
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
    configTree = kernel.ctx.get('configTree') as SqliteTree | undefined
    // 扫描结论挂到条目注记上：被强制禁用的条目要在插件列表里说清原因
    for (const issue of installIssues) configTree?.warnings.set(issue.entryId, issue.message)
  } catch (err) {
    log.error('[kernel] 配置树装载失败，本次会话无插件树：', err)
  }

  return { kernel, configTree, store, pluginMeta, installIssues, pluginsDir }
}
