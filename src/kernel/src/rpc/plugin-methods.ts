/* ============================================================
   plugins.* 的实现与参数守卫，与传输无关（#754）。

   原先这套住在桌面主进程里：守卫在 ipc/router.ts、实现在 ipc/methods.plugins.ts，
   两者都直接读桌面的模块级单例。服务器形态要的是同一批方法、同一套校验，
   只是传输从 Electron IPC 换成 HTTP/WS —— 所以把「校验 + 语义」搬到这里，
   两侧各自只写一个薄传输适配器。

   分层照旧，没有变：
   - 本模块只管**形状**（是不是字符串、是不是纯对象、树有没有超深）；
   - **语义**（__jsExpr、未知字段、重复 id、schema 匹配）归 SqliteTree，
     用与装载完全相同的一套规则拒绝，校验不复制两份；
   - 校验错误按契约**作为数据返回**（PluginValidationResult）给用户就地回显，
     装载失败**作为异常抛出**——前者是输入问题，后者是运行故障。

   信任边界也照旧：调用方传来的参数一律当作不可信输入独立校验，不因为
   「前端已经检查过」而省略。服务器形态下这一点更要紧——那头是网络。
   ============================================================ */

import type { PluginEntryInfo, PluginValidationResult, SqliteTree } from '../config/sqlite-tree.ts'
import type { ConfigEntry } from '../config/tree.ts'
import type { PluginMetaStore } from '../storage/store.ts'

export const ERR_INVALID_PARAMS = 'ERR_INVALID_PARAMS'
export const ERR_CAPABILITY_UNAVAILABLE = 'ERR_CAPABILITY_UNAVAILABLE'

/** 树导入嵌套上限。真实树两三层，16 层只为拦住构造出来的深递归载荷。 */
export const MAX_TREE_DEPTH = 16

export interface PluginTreeExportShape {
  version: 1
  /** 形状已由 asPluginTreeExport 逐条校验过；语义仍由 importTree 再判一次。 */
  entries: ConfigEntry[]
}

export function asString(params: unknown, key: string): string {
  const value = (params as Record<string, unknown> | null)?.[key]
  if (typeof value !== 'string') {
    throw new Error(`${ERR_INVALID_PARAMS}: ${key} must be a string`)
  }
  return value
}

export function asNumber(params: unknown, key: string): number {
  const value = (params as Record<string, unknown> | null)?.[key]
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${key} must be a finite number`)
  }
  return value
}

/**
 * 插件配置载荷：一律是纯对象。数组/原始值/null 在 schema 层面没有合法形状，
 * 在边界先挡掉；对象内部的语义归树层校验。
 */
export function asPluginConfig(params: unknown): Record<string, unknown> {
  const value = (params as Record<string, unknown> | null)?.['config']
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${ERR_INVALID_PARAMS}: config must be a plain object`)
  }
  return value as Record<string, unknown>
}

function assertTreeEntryShape(value: unknown, path: string, depth: number): void {
  if (depth > MAX_TREE_DEPTH) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path} exceeds max tree depth ${MAX_TREE_DEPTH}`)
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path} must be an object`)
  }
  const entry = value as Record<string, unknown>
  if (typeof entry.id !== 'string' || !entry.id) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path}.id must be a non-empty string`)
  }
  if (typeof entry.name !== 'string' || !entry.name) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path}.name must be a non-empty string`)
  }
  if (entry.disabled !== undefined && typeof entry.disabled !== 'boolean') {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path}.disabled must be a boolean`)
  }
  if (entry.children !== undefined) {
    if (!Array.isArray(entry.children)) {
      throw new Error(`${ERR_INVALID_PARAMS}: ${path}.children must be an array`)
    }
    entry.children.forEach((child, index) =>
      assertTreeEntryShape(child, `${path}.children[${index}]`, depth + 1),
    )
  }
  // 多余字段/重复 id/__jsExpr 故意不在这里查：那是语义校验，importTree 会用
  // 与装载完全相同的一套规则拒绝
}

export function asPluginTreeExport(params: unknown): PluginTreeExportShape {
  const value = (params as Record<string, unknown> | null)?.['tree']
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${ERR_INVALID_PARAMS}: tree must be an object`)
  }
  const tree = value as Record<string, unknown>
  if (tree.version !== 1) {
    throw new Error(`${ERR_INVALID_PARAMS}: unsupported tree export version ${String(tree.version)}`)
  }
  if (!Array.isArray(tree.entries)) {
    throw new Error(`${ERR_INVALID_PARAMS}: tree.entries must be an array`)
  }
  tree.entries.forEach((entry, index) => assertTreeEntryShape(entry, `entries[${index}]`, 0))
  return value as unknown as PluginTreeExportShape
}

/**
 * 宿主句柄。传函数而不是传值：树与持久层在会话中可能尚未就绪（storage 挂载
 * 失败、树装载失败），能力门槛必须每次调用现读，不能在构造时固化成快照。
 */
export interface PluginRpcDeps {
  configTree(): SqliteTree | null
  pluginMeta(): PluginMetaStore | null
}

export type RpcMethod = (params: unknown) => unknown | Promise<unknown>

export function createPluginMethods(deps: PluginRpcDeps): Record<string, RpcMethod> {
  /** 能力门槛：树不可达时全族抛同一个码，与能力位 plugins.manage 读同一事实源。 */
  function requireTree(): SqliteTree {
    const tree = deps.configTree()
    if (!tree) {
      throw new Error(`${ERR_CAPABILITY_UNAVAILABLE}: plugins.manage — kernel 配置树不可用`)
    }
    return tree
  }

  /** 变更后的条目回执：UI 开关不用再发一次 list 就能拿到最终运行态。 */
  function entryInfo(tree: SqliteTree, id: string): PluginEntryInfo {
    const info = tree.listEntries().find((entry) => entry.id === id)
    if (!info) throw new Error(`${ERR_INVALID_PARAMS}: unknown plugin entry ${id}`)
    return info
  }

  /** 写操作后 flush：树的 write 是防抖合并的，返回前落盘，调用方看到成功即已持久。 */
  async function setDisabledOption(id: string, disabled: true | null): Promise<PluginEntryInfo> {
    const tree = requireTree()
    // disabled: null = 删除该键（loader 的 isNullable 语义），回到默认启用。
    // 拉起失败时 Entry.update 自己回滚 options（树不被污染），错误照抛。
    await tree.update(id, { disabled })
    await tree.flush()
    return entryInfo(tree, id)
  }

  return {
    'plugins.list': () => requireTree().listEntries(),
    'plugins.enable': (p) => setDisabledOption(asString(p, 'id'), null),
    'plugins.disable': (p) => setDisabledOption(asString(p, 'id'), true),
    'plugins.updateConfig': async (p) => {
      const tree = requireTree()
      const id = asString(p, 'id')
      const config = asPluginConfig(p)
      const entry = tree.resolve(id) // 未知 id 在这里抛错
      if (entry.options.group) {
        // 组条目的 config 是子条目列表（loader 内部表示），从这里改会拆树
        throw new Error(
          `${ERR_INVALID_PARAMS}: entry ${id} is a group; groups carry no plugin config`,
        )
      }
      const result: PluginValidationResult = tree.validateConfig(entry.options.name, config)
      // 校验错误是数据不是异常：ok=false 时什么都没改，前端就地回显
      if (!result.ok) return result
      await tree.update(id, { config })
      await tree.flush()
      return result
    },
    'plugins.validateConfig': (p) =>
      requireTree().validateConfig(asString(p, 'name'), asPluginConfig(p)),
    // version 是 RPC 契约的事：kernel 树只管条目本身，这里包上版本号
    'plugins.exportTree': () => ({ version: 1, entries: requireTree().exportTree() }),
    'plugins.importTree': async (p) => {
      // last-good 快照落 PluginMetaStore；storage 失败的内存树会话拿不到 meta，
      // 导入照常（本次会话本来就不落盘）
      const exported = asPluginTreeExport(p)
      await requireTree().importTree(exported.entries, deps.pluginMeta() ?? undefined)
    },
  }
}
