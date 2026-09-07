/* ============================================================
   SqliteTree：把 loader 的 EntryTree 挂到 ConfigTreeStore 上（#699，D2）。

   形态对标 vendor 的 Include（文件版持久树）：SqliteTree 自己就是一棵
   EntryTree 插件，使用方先 ctx.plugin(Loader) 再 ctx.plugin(SqliteTree,
   { store })。没有选「继承 Loader 覆写 root/write」那条路，原因：
   Loader 是全局单例服务（构造期注册一堆 global 钩子、provide('loader')），
   把持久化揉进去会让「树」跟「装载服务」耦死；而 Include 型子树是
   vendor 已验证的扩展点（UPSTREAM.md 第 8/12/14 条的事务化/串行化/
   防抖写全是围着这个形态修的），entry 生命周期照样全部由 loader
   机制驱动（import/热更/回滚/启停都走 Entry.update 的分档策略）。

   两个边界只做两件事：
   - load：ConfigEntry[] → EntryOptions[]（children ↔ group:true+config
     子列表），交给 root.update() 复用 EntryGroup 的事务化 reconcile；
   - write：root.data → ConfigEntry[]，防抖合并后 store.save() 全量覆盖。

   v1 拒载面（D2/D5）：inject/isolate/intercept 等 loader 扩展字段、
   以及任意深度的 __jsExpr 表达式节点，装载与落库两个方向都拒绝——
   SqliteTree 永远不触达 evaluate，配置树里不存在可执行内容。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import { Context, Service, type Fiber } from '@deepseek-ai/cordis'
import { EntryGroup, EntryTree, type EntryOptions } from '@deepseek-ai/cordis-plugin-loader'
import type { ConfigEntry, ConfigTreeStore } from './tree.ts'

/** importTree 存 last-good 快照用的 PluginMetaStore 键（#705）。 */
export const CONFIG_TREE_LAST_GOOD_KEY = 'config-tree:last-good'

/** last-good 快照的最小落点：结构化成接口以免测试必须拉一整个 SQLite。 */
export interface LastGoodSink {
  set(key: string, value: unknown): void
}

/** plugins.list 的单条视图。桌面 contract.ts 手工镜像本形状，改动需两边同步。 */
export interface PluginEntryInfo {
  id: string
  name: string
  /** 树上的持久开关（用户意图）；state 才是运行事实。 */
  disabled: boolean
  state: 'active' | 'disabled' | 'error' | 'pending'
  error?: string
  config?: unknown
}

export interface PluginValidationError {
  path: string
  message: string
}

/** 校验错误作为数据返回（不抛异常）：前端配置编辑器要就地回显。 */
export interface PluginValidationResult {
  ok: boolean
  errors: PluginValidationError[]
}

/**
 * 条目运行态推导。FiberState 是 const enum——对开启 isolatedModules 的
 * 消费方（desktop 的 tsc）它是 ambient、成员不可引用，所以这里不 import
 * 枚举，而是照 Fiber._getState 的同一套事实源推导：_error 非空 = FAILED；
 * uid 未清且 store 已建 = 已装载（ACTIVE）；其余都算等待中。_error 是
 * 私有字段，但 FAILED 状态下它就是唯一的错误事实源（fiber.await() 会
 * rethrow 同一个值），这里只读不改。
 */
export function describeEntryState(
  fiber: Fiber | undefined,
  disabled: boolean,
): { state: PluginEntryInfo['state']; error?: string } {
  if (!fiber) return { state: disabled ? 'disabled' : 'pending' }
  const raw = (fiber as unknown as { _error?: unknown })._error
  if (raw !== undefined) {
    return { state: 'error', error: raw instanceof Error ? raw.message : String(raw) }
  }
  if (fiber.uid !== null && fiber.store !== undefined) return { state: 'active' }
  return { state: 'pending' }
}

/** ConfigEntry 允许的键。多出来的一律拒载，见 assertEntryKeys。 */
const CONFIG_ENTRY_KEYS = new Set(['id', 'name', 'config', 'disabled', 'children'])

/** 序列化方向 EntryOptions 允许的键：group 是内部表示（children 的等价物）。 */
const ENTRY_OPTIONS_KEYS = new Set(['id', 'name', 'config', 'disabled', 'group'])

/**
 * 深扫 config 载荷里的 __jsExpr 节点（D5：!!js 封死）。
 * include 的 YAML 方言允许配置里带可执行表达式，SqliteTree 的树来自
 * SQLite/导入数据，任何位置出现表达式节点都视为投毒，带路径拒绝。
 */
function assertNoJsExpr(value: unknown, path: string): void {
  if (!value || typeof value !== 'object') return
  if ('__jsExpr' in value) {
    throw new Error(`配置树 ${path} 含 __jsExpr 表达式节点，SqliteTree 拒绝装载可执行配置`)
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoJsExpr(item, `${path}[${index}]`))
  } else {
    for (const [key, item] of Object.entries(value)) {
      assertNoJsExpr(item, `${path}.${key}`)
    }
  }
}

function assertEntryKeys(entry: object, allowed: Set<string>, path: string): void {
  for (const key of Object.keys(entry)) {
    if (!allowed.has(key)) {
      throw new Error(
        `配置树条目 ${path} 含不支持的字段 "${key}"（v1 不支持 inject/isolate/intercept 等 loader 扩展，遇到即拒载）`,
      )
    }
  }
}

/**
 * 装载边界：ConfigEntry → EntryOptions。
 * children 的存在与否是「组」的唯一判据：有 children 就转成
 * group:true + config=子条目列表（loader 里组的 config 就是子条目
 * 列表，见 vendor loader Group）；组条目的 name 原样透传（组行需要
 * 一个可导入的 specifier，如 cordis:group）。
 */
function toEntryOptions(entry: ConfigEntry, path: string, seen: Set<string>): EntryOptions {
  assertEntryKeys(entry, CONFIG_ENTRY_KEYS, path)
  if (typeof entry.id !== 'string' || !entry.id) {
    throw new Error(`配置树条目 ${path} 缺少字符串 id`)
  }
  if (entry.id.includes(':')) {
    // ':' 是 loader 的嵌套树 id 分隔符（EntryTree.sep），resolve 会把它
    // 当子树路径拆开；组内条目其实平铺在同一个 store 里，id 带冒号必错
    throw new Error(`配置树条目 ${path} 的 id "${entry.id}" 含保留分隔符 ":"`)
  }
  if (seen.has(entry.id)) {
    // loader 的 store 是全树平铺的字典，跨组重复 id 会让后者静默
    // 抢占（reparent）前者的条目，必须在装载前挡下
    throw new Error(`配置树条目 ${path} 的 id "${entry.id}" 在树内重复`)
  }
  seen.add(entry.id)
  if (typeof entry.name !== 'string' || !entry.name) {
    throw new Error(`配置树条目 ${path} 缺少字符串 name`)
  }
  if (entry.disabled !== undefined && typeof entry.disabled !== 'boolean') {
    throw new Error(`配置树条目 ${path} 的 disabled 必须是布尔值`)
  }
  if (entry.children !== undefined && entry.config !== undefined) {
    throw new Error(`配置树条目 ${path} 同时带 children 与 config（组的配置就是子条目列表，两者互斥）`)
  }

  const options: EntryOptions = { id: entry.id, name: entry.name }
  if (entry.children !== undefined) {
    if (!Array.isArray(entry.children)) {
      throw new Error(`配置树条目 ${path} 的 children 必须是数组`)
    }
    options.group = true
    options.config = entry.children.map((child, index) =>
      toEntryOptions(child, childPath(path, child, index), seen),
    )
  } else if (entry.config !== undefined) {
    assertNoJsExpr(entry.config, `${path}.config`)
    options.config = entry.config
  }
  if (entry.disabled !== undefined) options.disabled = entry.disabled
  return options
}

/**
 * 落库边界：EntryOptions → ConfigEntry（toEntryOptions 的逆变换）。
 * 组条目的 config 数组与 EntryGroup.data 是同一份引用（loader 的
 * 事务化 update 保证 identity），所以直接递归它就是当前真相。
 * 运行期若有钩子往 options 里塞了 v1 不支持的字段，这里同样拒绝——
 * 静默丢字段落库会让下次装载读到与真相不符的树。
 */
function toConfigEntry(options: EntryOptions, path: string): ConfigEntry {
  assertEntryKeys(options, ENTRY_OPTIONS_KEYS, path)
  const entry: ConfigEntry = { id: options.id, name: options.name }
  if (options.group) {
    const children = (options.config ?? []) as EntryOptions[]
    entry.children = children.map((child, index) =>
      toConfigEntry(child, childPath(path, child, index)),
    )
  } else if (options.config !== undefined) {
    assertNoJsExpr(options.config, `${path}.config`)
    entry.config = options.config
  }
  if (typeof options.disabled === 'boolean') entry.disabled = options.disabled
  return entry
}

function childPath(parent: string, child: { id?: unknown }, index: number): string {
  const label = typeof child?.id === 'string' && child.id ? child.id : `#${index}`
  return parent ? `${parent}.${label}` : label
}

export namespace SqliteTree {
  export interface Config {
    /** 树的持久化后座：SqliteConfigTreeStore（生产）或 Memory（测试）。 */
    store: ConfigTreeStore
  }
}

/** ConfigTreeStore 持久化的 loader 条目树。用法：先装 Loader，再装它。 */
export class SqliteTree extends EntryTree {
  static inject = ['loader']

  // 树载体标记（Group/Include 同款）：本插件的 config 是 { store } 与
  // 子条目列表的容器，loader 的 internal/config 插值必须对它保持字面
  // 透传——valueMap 递归克隆会把 store 实例克隆成失去原型的空壳
  static readonly [EntryGroup.key] = true

  #dirty = false
  #writeTask: NodeJS.Timeout | undefined
  #writeQueue: Promise<void> = Promise.resolve()

  constructor(ctx: Context, public config: SqliteTree.Config) {
    super(ctx)
  }

  async *[Service.init](): AsyncGenerator<() => Promise<void>, void, void> {
    const entries = await this.config.store.load()
    const data = entries.map((entry, index) =>
      toEntryOptions(entry, childPath('', entry, index), new Set()),
    )
    // 挂成服务：桌面侧（PR-2）要拿树的句柄做 engine spec 注入与
    // plugins.* IPC（PR-3），reflect 的严格模式顺便保证「树没就绪
    // 就拿不到句柄」
    this.ctx.provide('configTree', this)
    yield () => this.stop()
    // 复用 EntryGroup 的事务化 reconcile：并发拉起、失败整体回滚
    await this.root.update(data)
  }

  async stop(): Promise<void> {
    await this.root.stop()
    // root.stop 只 dispose 不改 data，最后一次 flush 落的仍是完整的树
    await this.flush()
  }

  override import(name: string, getOuterStack?: () => string[]) {
    // 基类对未知的 cordis: builtin 返回 undefined，这个 undefined 会一路
    // 走到 Entry.update「先 dispose 旧 fiber 再 start」之后才炸，回滚只能
    // 重建一个新 fiber。在 import 阶段就抛错，Entry.update 会在动手前
    // 失败——原 fiber 原样活着，这正是坏包回滚想要的语义。
    if (name.startsWith('cordis:') && !(name.slice(7) in this.ctx.loader.builtins)) {
      throw new Error(`未知的内置插件 "${name}"（未在 loader.builtins 注册）`)
    }
    return super.import(name, getOuterStack)
  }

  /** plugins.list（#705）：树条目 + 运行态的扁平视图。 */
  listEntries(): PluginEntryInfo[] {
    return [...this.entries()].map((entry) => {
      const { state, error } = describeEntryState(entry.fiber, entry.disabled)
      const info: PluginEntryInfo = {
        id: entry.id,
        name: entry.options.name,
        disabled: Boolean(entry.options.disabled),
        state,
      }
      if (error !== undefined) info.error = error
      // 组条目的 config 是子条目列表（loader 内部表示），不当作插件配置暴露
      if (!entry.options.group && entry.options.config !== undefined) {
        info.config = entry.options.config
      }
      return info
    })
  }

  /**
   * plugins.validateConfig（#705）：在 kernel 进程内做 schema 校验，错误
   * 作为数据返回。内置插件（cordis:）从 loader.builtins 查 Config schema；
   * 插件没有 Config 或是非内置 specifier（三方 npm 包，拿不到 schema）时
   * 无校验即通过——装载时 cordis 还会用插件自己的 Config 再校一遍兜底。
   */
  validateConfig(name: string, config: unknown): PluginValidationResult {
    const errors: PluginValidationError[] = []
    const push = (cause: unknown): void => {
      // path 统一留空：schemastery 的报错文案自带字段路径，拆出来反而丢上下文
      errors.push({ path: '', message: cause instanceof Error ? cause.message : String(cause) })
    }
    try {
      // D5：__jsExpr 在任何入口都封死，校验层也要把它当错误回显而不是放过
      assertNoJsExpr(config, 'config')
    } catch (cause) {
      push(cause)
    }
    if (name.startsWith('cordis:')) {
      const plugin = this.ctx.loader.builtins[name.slice(7)] as { Config?: unknown } | undefined
      if (!plugin) {
        push(new Error(`未知的内置插件 "${name}"（未在 loader.builtins 注册）`))
      } else if (typeof plugin.Config === 'function') {
        // schemastery 的 schema 本身可调用：调用即校验（返回补全默认值的
        // 副本）。这里只收集错误，不把补全后的值写回树——树存用户写的原文。
        try {
          ;(plugin.Config as (value: unknown) => unknown)(config)
        } catch (cause) {
          push(cause)
        }
      }
    }
    return { ok: errors.length === 0, errors }
  }

  /** plugins.exportTree（#705）：当前树的序列化快照（与落库走同一变换）。 */
  exportTree(): ConfigEntry[] {
    return this.root.data.map((options, index) =>
      toConfigEntry(options, childPath('', options, index)),
    )
  }

  /**
   * plugins.importTree（#705）：全量替换整树。
   *
   * 三步走，顺序即安全设计：
   * 1. 语义校验完全复用装载边界的 toEntryOptions（__jsExpr / 未知字段 /
   *    重复 id / 形状错误一律抛错）——校验不过时树没有被碰过；
   * 2. 把替换前的树快照写进 last-good（PluginMetaStore），这是「全量覆盖
   *    可以清空配置」这条风险的持久保险：哪怕后面的回滚也失败，用户数据
   *    仍然躺在 KV 里可以人工恢复；
   * 3. root.update 事务化 reconcile。它失败时自己会把内存树滚回旧状态，
   *    这里再按 last-good 重放一次做兜底（覆盖「内部回滚半途而废」的残局）。
   */
  async importTree(entries: ConfigEntry[], lastGood?: LastGoodSink): Promise<void> {
    const seen = new Set<string>()
    const next = entries.map((entry, index) =>
      toEntryOptions(entry, childPath('', entry, index), seen),
    )
    const snapshot = this.exportTree()
    lastGood?.set(CONFIG_TREE_LAST_GOOD_KEY, snapshot)
    try {
      await this.root.update(next)
    } catch (error) {
      try {
        await this.root.update(
          snapshot.map((entry, index) => toEntryOptions(entry, childPath('', entry, index), new Set())),
        )
      } catch (rollbackError) {
        throw new AggregateError([error, rollbackError], '导入失败且 last-good 回滚未完全成功')
      }
      // 回滚成功后同样落一次库：部分拉起可能已触发过 write，把磁盘拉回真相
      this.write()
      await this.flush().catch(() => {
        /* flush 内部已记日志；这里不让落库失败盖掉导入失败的原始错误 */
      })
      throw error
    }
    this.write()
    await this.flush()
  }

  /**
   * 防抖合并写（why：写放大）。loader 的每个树操作都会调一次 write——
   * create/update/remove 各一次，internal/update 钩子回写 config 又一次，
   * 一轮 root.update 重放整树更是 O(条目数) 次。store.save 本身是全量
   * 覆盖语义，最后一份快照就是完整真相，中间态可以无损丢弃，所以这里
   * 只置脏标记，用一个 0ms 定时器把同一轮事件循环里的写合并成一次。
   */
  write(): void {
    this.#dirty = true
    this.#writeTask ??= setTimeout(() => {
      this.#writeTask = undefined
      void this.#drain()
    }, 0)
  }

  /** 立刻落库：测试断言前与 stop() 前调用。返回值会把落库失败抛给调用方。 */
  flush(): Promise<void> {
    if (this.#writeTask !== undefined) {
      clearTimeout(this.#writeTask)
      this.#writeTask = undefined
    }
    return this.#drain()
  }

  #drain(): Promise<void> {
    if (this.#dirty) {
      this.#dirty = false
      // 串行链：save 是异步的，两次覆盖写交错会让旧快照压过新快照
      const run = this.#writeQueue.then(
        () => this.#save(),
        () => this.#save(),
      )
      this.#writeQueue = run
      // 后台定时器路径没有调用方接错误，就地记日志；flush() 的调用方
      // 拿同一个 promise，照样能收到 rejection
      void run.catch((error) => {
        this.ctx.root.logger?.('loader').warn('配置树落库失败')
        this.ctx.root.logger?.('loader').warn(error)
      })
    }
    return this.#writeQueue
  }

  async #save(): Promise<void> {
    // 序列化放在真正写的时刻：root.data 是就地变更的活数组，写入点
    // 取快照才能保证落的是合并后的最终状态
    await this.config.store.save(this.exportTree())
  }
}
