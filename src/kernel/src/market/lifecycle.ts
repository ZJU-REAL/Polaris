/* ============================================================
   安装生命周期接线（#708，市场 PR-6）：把 #700 的纯函数安装引擎接到
   持久层与配置树上。

   职责边界：
   - installPlugin/uninstallPlugin 只管盘面（#700，保持纯函数）；
   - 本模块负责「装完之后」的三件事：InstallRecord 落 PluginMetaStore、
     配置树挂 disabled 条目（装/启分离：安装绝不自动运行代码）、卸载时
     反向拆干净；
   - 装载前哈希复核分两个挂点（why 见各函数头注）：启动扫描
     verifyInstalledEntries（树装载之前跑，能安全地强制 disabled）+
     createImportGuard（enable 时刻的最后一道闸，覆盖会话中途被篡改）。

   metaStore 用结构化接口（PluginMetaLike）而不是直接依赖 storage 插件：
   测试给个 Map 就能跑全链路，也保住 market/ 子树不依赖 node:sqlite。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import type { EntryOptions } from '@deepseek-ai/cordis-plugin-loader'
import { rm } from 'node:fs/promises'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import type { SqliteTree } from '../config/sqlite-tree.ts'
import type { ConfigEntry, ConfigTreeStore } from '../config/tree.ts'
import { MarketError, type InstallRecord } from './contract.ts'
import { installPlugin, uninstallPlugin, verifyEntryHash, type InstallPluginOptions } from './install.ts'

/** InstallRecord 在 PluginMetaStore 里的键前缀；每个包一条记录（单版本策略）。 */
export const INSTALL_RECORD_PREFIX = 'market:install:'

export function installRecordKey(name: string): string {
  return `${INSTALL_RECORD_PREFIX}${name}`
}

/** PluginMetaStore 的结构化子集（storage/store.ts 的 PluginMetaStore 天然满足）。 */
export interface PluginMetaLike {
  get(key: string): unknown
  set(key: string, value: unknown): void
  delete(key: string): boolean
  list(): string[]
}

/**
 * npm 包名 → 树条目 id。':' 是 loader 的子树分隔符、'/' 会让 id 读起来
 * 像路径，都不能进 id；scoped 包剥掉 @、'/' 换 '--'（双横线在合法包名里
 * 不会与单横线歧义到产生实际碰撞）。
 */
export function packageEntryId(name: string): string {
  return name.replace(/^@/, '').replace(/\//g, '--')
}

/** 安装记录对应的入口文件 file:// URL——树条目的 name 就存它。 */
export function entryFileUrl(record: InstallRecord): string {
  return pathToFileURL(join(record.dir, record.entry)).href
}

function readRecord(metaStore: PluginMetaLike, name: string): InstallRecord | undefined {
  const raw = metaStore.get(installRecordKey(name))
  if (!raw || typeof raw !== 'object') return undefined
  const record = raw as Partial<InstallRecord>
  // KV 里的值可能被外部改坏；字段不齐的记录当不存在处理，宁可重装
  for (const key of ['name', 'version', 'entry', 'tarballSha512', 'entrySha256', 'dir'] as const) {
    if (typeof record[key] !== 'string' || !record[key]) return undefined
  }
  return record as InstallRecord
}

export interface InstallAndRegisterOptions extends InstallPluginOptions {
  tree: SqliteTree
  metaStore: PluginMetaLike
}

export interface InstallAndRegisterResult {
  record: InstallRecord
  /** 配置树条目 id（packageEntryId(name)）。 */
  entryId: string
}

/**
 * 安装并登记：installPlugin → InstallRecord 落 metaStore → 树上挂
 * disabled 条目。装/启分离（Koishi 惯例）：安装后的代码一行都不执行，
 * 由用户在插件列表里显式启用。
 *
 * 同名重装（含升级）取单版本策略：新版本装成后删旧版本目录、树条目
 * 改指新入口，并且**强制回到 disabled**——升级得到的是新代码，自动
 * 沿用旧的启用状态等于「更新即执行」，正是装启分离要挡的事。多版本
 * 并存对 v1 没有消费方（树条目只有一个 name），只会留下孤儿目录。
 */
export async function installAndRegister(options: InstallAndRegisterOptions): Promise<InstallAndRegisterResult> {
  const { tree, metaStore, ...installOptions } = options
  const previous = readRecord(metaStore, installOptions.name)

  const record = await installPlugin(installOptions)
  installOptions.onPhase?.('register')

  // 旧版本目录在新版本完整落盘之后才删：中途失败时旧安装保持可用
  if (previous && previous.dir !== record.dir) {
    await rm(previous.dir, { recursive: true, force: true })
  }
  metaStore.set(installRecordKey(record.name), record)

  const entryId = packageEntryId(record.name)
  const url = entryFileUrl(record)
  const existing = tree.store[entryId]
  if (existing) {
    // 就地更新而不是 remove+create：保住条目的 config 与树上的位置。
    // 直接调 Entry.update（force）——EntryTree.update 不接受 name 变更。
    // 条目若在运行，disabled: true 会让它先被安全地停掉。
    await existing.update({ name: url, disabled: true }, false, true)
    tree.write()
  } else {
    // ensureId 尊重传入的 id：条目 id 用包名规范化，卸载/复核都能反查
    const entryOptions: EntryOptions = { id: entryId, name: url, disabled: true }
    await tree.create(entryOptions)
  }
  await tree.flush()
  return { record, entryId }
}

export interface UninstallAndRemoveOptions {
  /** npm 包名或树条目 id（packageEntryId 形态），二者皆可，见 resolveInstallName。 */
  name: string
  pluginsDir: string
  tree: SqliteTree
  metaStore: PluginMetaLike
}

/** 卸载结果作为数据返回：enabled 拒卸是正常业务分支，不是异常。 */
export type UninstallOutcome =
  | { ok: true }
  | { ok: false; code: 'plugin-enabled' | 'not-installed'; message: string }

/**
 * 卸载入口的双解析：输入先当 npm 包名直查安装记录；未命中再扫全部
 * 安装记录，找 packageEntryId(record.name) 等于输入的那条——即输入是
 * 树条目 id 的情形。为什么要兼容 id：前端插件列表（plugins.list）里
 * 可靠可得的只有条目 id（条目 name 存的是 file:// 入口 URL，市场包名
 * 根本不在里面），而从 id 反推包名对含 '--' 的无 scope 包名有歧义，
 * 只有持有安装记录的这一侧能无歧义地解析。都未命中时原样返回，
 * 让后续统一走 not-installed。
 */
function resolveInstallName(metaStore: PluginMetaLike, input: string): string {
  if (readRecord(metaStore, input)) return input
  for (const key of metaStore.list()) {
    if (!key.startsWith(INSTALL_RECORD_PREFIX)) continue
    const record = readRecord(metaStore, key.slice(INSTALL_RECORD_PREFIX.length))
    if (record && packageEntryId(record.name) === input) return record.name
  }
  return input
}

/**
 * 卸载并拆登记：树条目仍处于启用态时拒绝（用户先禁用、看清后果再卸，
 * 也避免「卸载顺手停掉正在运行的东西」这种隐式副作用）；disabled 或
 * 条目已被用户删掉时：移除树条目 → 删安装目录 → 删记录。
 */
export async function uninstallAndRemove(options: UninstallAndRemoveOptions): Promise<UninstallOutcome> {
  const { pluginsDir, tree, metaStore } = options
  const name = resolveInstallName(metaStore, options.name)
  const entryId = packageEntryId(name)
  const entry = tree.store[entryId]
  const record = readRecord(metaStore, name)

  if (!entry && !record) {
    return { ok: false, code: 'not-installed', message: `插件 ${name} 未安装` }
  }
  if (entry && !entry.options.disabled) {
    return { ok: false, code: 'plugin-enabled', message: `插件 ${name} 仍处于启用状态，请先禁用再卸载` }
  }
  if (entry) await tree.remove(entryId)
  await uninstallPlugin({ name, pluginsDir })
  metaStore.delete(installRecordKey(name))
  await tree.flush()
  return { ok: true }
}

export interface VerifyInstalledOptions {
  metaStore: PluginMetaLike
  /** 直接操作持久 store（树装载之前跑），不经过活树。 */
  store: ConfigTreeStore
  /** 打包态 true：哈希不符强制 disabled；开发态 false：仅报告。 */
  strict: boolean
}

export interface InstallVerifyIssue {
  name: string
  entryId: string
  message: string
  /** strict 下是否真的把树条目改成了 disabled（条目不存在/本就 disabled 时 false）。 */
  forcedDisabled: boolean
}

function tamperMessage(record: InstallRecord): string {
  return `插件 ${record.name}@${record.version} 的入口文件与安装记录的哈希不符（文件被改动或删除），拒绝装载`
}

/**
 * 启动扫描（哈希复核的主挂点）：遍历 metaStore 全部安装记录逐条
 * verifyEntryHash，不符的在**树装载之前**把持久 store 里指向该入口的
 * 条目改成 disabled。
 *
 * 为什么选启动扫描而不是只靠 import 覆写：SqliteTree 初始化用
 * EntryGroup.update 的事务化 reconcile，启动时任何一个条目 import 抛错
 * 会让整树回滚（kernel.ts：本次会话无插件树）——被篡改的应当只有它
 * 自己被禁用，不能拖垮其他插件。在树装载前改 store 是唯一能表达
 * 「强制 disabled + 其余照常」的位置。import 覆写（createImportGuard）
 * 作为第二道闸保留，盯的是会话中途篡改后再 enable 的窗口。
 */
export async function verifyInstalledEntries(options: VerifyInstalledOptions): Promise<InstallVerifyIssue[]> {
  const { metaStore, store, strict } = options
  const issues: InstallVerifyIssue[] = []
  let entries: ConfigEntry[] | undefined
  let mutated = false

  const disableMatching = (list: ConfigEntry[], entryId: string, url: string): boolean => {
    let changed = false
    for (const entry of list) {
      // 双判据：id 是安装时登记的规范化包名，name 是入口 URL——
      // 用户手工复制过条目时按 name 也能兜住
      if ((entry.id === entryId || entry.name === url) && entry.disabled !== true) {
        entry.disabled = true
        changed = true
      }
      if (entry.children && disableMatching(entry.children, entryId, url)) changed = true
    }
    return changed
  }

  for (const key of metaStore.list()) {
    if (!key.startsWith(INSTALL_RECORD_PREFIX)) continue
    const record = readRecord(metaStore, key.slice(INSTALL_RECORD_PREFIX.length))
    if (!record) continue
    if (await verifyEntryHash(record)) continue

    const entryId = packageEntryId(record.name)
    let forcedDisabled = false
    if (strict) {
      entries ??= await store.load()
      if (disableMatching(entries, entryId, entryFileUrl(record))) {
        forcedDisabled = true
        mutated = true
      }
    }
    issues.push({ name: record.name, entryId, message: tamperMessage(record), forcedDisabled })
  }
  if (mutated && entries) await store.save(entries)
  return issues
}

export interface ImportGuardOptions {
  metaStore: PluginMetaLike
  /** 打包态 true：不符直接拒绝 import；开发态 false：仅 warn 后放行。 */
  strict: boolean
  warn?: (message: string) => void
}

/**
 * enable 时刻的复核闸（哈希复核的第二挂点），挂在 SqliteTree.Config 的
 * guardImport 上。启动扫描盖不住的窗口是「会话运行中文件被篡改、用户
 * 随后点启用」——enable 走单条目的 Entry.update，import 抛错只回滚它
 * 自己（options 滚回 disabled），错误经 IPC 原样交给用户，没有整树
 * 回滚的风险。不在安装记录里的 specifier（内置/开发者手挂的本地文件）
 * 不归它管，直接放行。
 */
export function createImportGuard(options: ImportGuardOptions): (name: string) => Promise<void> {
  const { metaStore, strict, warn } = options
  return async (name: string) => {
    if (!name.startsWith('file:')) return
    for (const key of metaStore.list()) {
      if (!key.startsWith(INSTALL_RECORD_PREFIX)) continue
      const record = readRecord(metaStore, key.slice(INSTALL_RECORD_PREFIX.length))
      if (!record || entryFileUrl(record) !== name) continue
      if (await verifyEntryHash(record)) return
      if (strict) throw new MarketError('integrity-mismatch', tamperMessage(record))
      warn?.(tamperMessage(record))
      return
    }
  }
}
