/* 安装生命周期（#708）的单元测试：fixture registry（helpers/market-fixture）
   + 真 Loader/SqliteTree（Memory store）+ Map 版 PluginMetaLike，跑
   「安装→登记→启用→复核→卸载」的完整链路。打包态/开发态语义用
   strict 参数注入模拟，不依赖 electron。 */
import { existsSync } from 'node:fs'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import {
  INSTALL_RECORD_PREFIX,
  Loader,
  MemoryConfigTreeStore,
  SqliteTree,
  createImportGuard,
  createKernel,
  entryFileUrl,
  installAndRegister,
  installRecordKey,
  packageEntryId,
  registerBuiltins,
  uninstallAndRemove,
  verifyInstalledEntries,
  type ConfigEntry,
  type InstallPhase,
  type InstallRecord,
  type Kernel,
  type PluginMetaLike,
} from '../src/index.ts'
import { HELLO_ENTRY, helloTgz, registryFetch, REGISTRY } from './helpers/market-fixture.ts'

const NAME = 'polaris-plugin-hello'
const KEY = installRecordKey(NAME)

/* ---------- 临时目录与 kernel 记账 ---------- */

let dirs: string[] = []
let kernels: Kernel[] = []
afterEach(async () => {
  for (const kernel of kernels) await kernel.stop().catch(() => {})
  kernels = []
  await Promise.all(dirs.map((dir) => rm(dir, { recursive: true, force: true })))
  dirs = []
})

async function freshPluginsDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), 'polaris-market-lc-'))
  dirs.push(dir)
  return dir
}

function memMeta(): PluginMetaLike {
  const map = new Map<string, unknown>()
  return {
    get: (key) => map.get(key),
    set: (key, value) => void map.set(key, JSON.parse(JSON.stringify(value))),
    delete: (key) => map.delete(key),
    list: () => [...map.keys()].sort(),
  }
}

interface Harness {
  kernel: Kernel
  tree: SqliteTree
  store: MemoryConfigTreeStore
  metaStore: PluginMetaLike
}

async function bootTree(guardImport?: SqliteTree.Config['guardImport']): Promise<Harness> {
  const kernel = createKernel({ name: 'market-lifecycle-test' })
  kernels.push(kernel)
  await kernel.start()
  await kernel.ctx.plugin(Loader)
  registerBuiltins(kernel.ctx.loader)
  const store = new MemoryConfigTreeStore()
  await kernel.ctx.plugin(SqliteTree, { store, guardImport })
  const tree = kernel.ctx.get('configTree') as SqliteTree
  return { kernel, tree, store, metaStore: memMeta() }
}

async function installHello(h: Harness, pluginsDir: string, options: {
  version?: string
  entryData?: string
  onPhase?: (phase: InstallPhase) => void
} = {}) {
  const version = options.version ?? '1.0.0'
  return installAndRegister({
    name: NAME,
    version,
    registryUrl: REGISTRY,
    pluginsDir,
    fetchImpl: registryFetch(helloTgz({ version, entryData: options.entryData }), { version }),
    onPhase: options.onPhase,
    tree: h.tree,
    metaStore: h.metaStore,
  })
}

/* ---------- 安装登记 ---------- */

describe('installAndRegister', () => {
  it('records to the meta store and adds a disabled tree entry without running code', async () => {
    const h = await bootTree()
    const pluginsDir = await freshPluginsDir()
    const phases: InstallPhase[] = []
    const { record, entryId } = await installHello(h, pluginsDir, { onPhase: (p) => phases.push(p) })

    // 进度点按顺序各来一次：下载/校验/解压/登记
    expect(phases).toEqual(['download', 'verify', 'extract', 'register'])
    expect(entryId).toBe(NAME)
    expect(h.metaStore.get(KEY)).toMatchObject({ name: NAME, version: '1.0.0' })

    // 装/启分离：条目在树上、指向 file:// 入口、disabled、零 fiber
    const entry = h.tree.store[NAME]!
    expect(entry.options.name).toBe(entryFileUrl(record))
    expect(entry.options.name.startsWith('file://')).toBe(true)
    expect(entry.options.disabled).toBe(true)
    expect(entry.fiber).toBeUndefined()

    // flush 已发生：持久 store 里就是这份 disabled 条目
    const persisted = await h.store.load()
    expect(persisted).toContainEqual({ id: NAME, name: entryFileUrl(record), disabled: true })

    // 启用后真的能装载（file:// 动态 import 全链路）
    await h.tree.update(NAME, { disabled: null })
    expect(h.tree.store[NAME]!.fiber).toBeTruthy()
  })

  it('reinstalling a newer version drops the old dir, repoints the entry and forces disabled', async () => {
    const h = await bootTree()
    const pluginsDir = await freshPluginsDir()
    const first = await installHello(h, pluginsDir)
    await h.tree.update(NAME, { disabled: null }) // 用户启用了 1.0.0
    expect(h.tree.store[NAME]!.fiber).toBeTruthy()

    const second = await installHello(h, pluginsDir, { version: '1.1.0' })
    // 单版本策略：旧版本目录清掉，记录/树条目全部指向新版本
    expect(existsSync(first.record.dir)).toBe(false)
    expect(existsSync(second.record.dir)).toBe(true)
    expect(h.metaStore.get(KEY)).toMatchObject({ version: '1.1.0' })
    const entry = h.tree.store[NAME]!
    expect(entry.options.name).toBe(entryFileUrl(second.record))
    // 升级 = 新代码，强制回 disabled 由用户重新启用，运行中的旧 fiber 已停
    expect(entry.options.disabled).toBe(true)
    expect(entry.fiber).toBeUndefined()
  })
})

/* ---------- 卸载 ---------- */

describe('uninstallAndRemove', () => {
  it('refuses to uninstall while the entry is enabled (error as data)', async () => {
    const h = await bootTree()
    const pluginsDir = await freshPluginsDir()
    const { record } = await installHello(h, pluginsDir)
    await h.tree.update(NAME, { disabled: null })

    const outcome = await uninstallAndRemove({ name: NAME, pluginsDir, tree: h.tree, metaStore: h.metaStore })
    expect(outcome).toEqual({ ok: false, code: 'plugin-enabled', message: expect.stringContaining('禁用') })
    // 拒卸即无副作用：记录、盘面、树条目原样
    expect(h.metaStore.get(KEY)).toBeDefined()
    expect(existsSync(record.dir)).toBe(true)
    expect(h.tree.store[NAME]).toBeDefined()
  })

  it('uninstalls a disabled plugin: tree entry, files and record all go away', async () => {
    const h = await bootTree()
    const pluginsDir = await freshPluginsDir()
    await installHello(h, pluginsDir)

    const outcome = await uninstallAndRemove({ name: NAME, pluginsDir, tree: h.tree, metaStore: h.metaStore })
    expect(outcome).toEqual({ ok: true })
    expect(h.tree.store[NAME]).toBeUndefined()
    expect(existsSync(join(pluginsDir, NAME))).toBe(false)
    expect(h.metaStore.get(KEY)).toBeUndefined()
    expect(await h.store.load()).toEqual([])
  })

  it('reports a plugin that was never installed as data', async () => {
    const h = await bootTree()
    const outcome = await uninstallAndRemove({
      name: 'polaris-plugin-ghost',
      pluginsDir: await freshPluginsDir(),
      tree: h.tree,
      metaStore: h.metaStore,
    })
    expect(outcome).toMatchObject({ ok: false, code: 'not-installed' })
  })
})

/* ---------- 启动扫描复核 ---------- */

describe('verifyInstalledEntries', () => {
  async function tamperedSetup() {
    const h = await bootTree()
    const pluginsDir = await freshPluginsDir()
    const { record } = await installHello(h, pluginsDir)
    await h.tree.update(NAME, { disabled: null }) // 启用后才有「强制禁用」可言
    await h.tree.flush()
    await writeFile(join(record.dir, record.entry), 'module.exports = { name: "evil", apply() {} }\n')
    return { h, record }
  }

  it('strict mode force-disables the persisted entry and reports the issue', async () => {
    const { h } = await tamperedSetup()
    const issues = await verifyInstalledEntries({ metaStore: h.metaStore, store: h.store, strict: true })
    expect(issues).toEqual([
      { name: NAME, entryId: NAME, message: expect.stringContaining('哈希不符'), forcedDisabled: true },
    ])
    const persisted = await h.store.load()
    expect(persisted.find((e: ConfigEntry) => e.id === NAME)?.disabled).toBe(true)
  })

  it('dev mode only reports and leaves the store untouched', async () => {
    const { h } = await tamperedSetup()
    const issues = await verifyInstalledEntries({ metaStore: h.metaStore, store: h.store, strict: false })
    expect(issues).toMatchObject([{ name: NAME, forcedDisabled: false }])
    const persisted = await h.store.load()
    expect(persisted.find((e: ConfigEntry) => e.id === NAME)?.disabled).toBeUndefined()
  })

  it('an intact install passes without issues', async () => {
    const h = await bootTree()
    await installHello(h, await freshPluginsDir())
    await expect(
      verifyInstalledEntries({ metaStore: h.metaStore, store: h.store, strict: true }),
    ).resolves.toEqual([])
  })
})

/* ---------- enable 时刻的 import 闸 ---------- */

describe('createImportGuard', () => {
  it('strict guard rejects enabling a tampered entry; the tree entry stays disabled', async () => {
    const metaStore = memMeta()
    const h = await bootTree(createImportGuard({ metaStore, strict: true }))
    h.metaStore = metaStore
    const pluginsDir = await freshPluginsDir()
    const { record } = await installHello(h, pluginsDir)
    await writeFile(join(record.dir, record.entry), 'module.exports = { name: "evil", apply() {} }\n')

    await expect(h.tree.update(NAME, { disabled: null })).rejects.toThrow(/哈希不符/)
    // Entry.update 在 import 阶段失败会把 options 滚回原样：仍是 disabled
    expect(h.tree.store[NAME]!.options.disabled).toBe(true)
    expect(h.tree.store[NAME]!.fiber).toBeUndefined()
  })

  it('dev guard only warns and lets the entry load', async () => {
    const metaStore = memMeta()
    const warnings: string[] = []
    const h = await bootTree(createImportGuard({ metaStore, strict: false, warn: (m) => warnings.push(m) }))
    h.metaStore = metaStore
    const pluginsDir = await freshPluginsDir()
    const { record } = await installHello(h, pluginsDir)
    await writeFile(join(record.dir, record.entry), HELLO_ENTRY + '// dev tweak\n')

    await h.tree.update(NAME, { disabled: null })
    expect(h.tree.store[NAME]!.fiber).toBeTruthy()
    expect(warnings).toEqual([expect.stringContaining('哈希不符')])
  })

  it('ignores specifiers that are not market installs', async () => {
    const guard = createImportGuard({ metaStore: memMeta(), strict: true })
    await expect(guard('cordis:sources')).resolves.toBeUndefined()
    await expect(guard('file:///somewhere/else.js')).resolves.toBeUndefined()
  })
})

/* ---------- 诊断注记与 id 规范化 ---------- */

describe('diagnostics', () => {
  it('listEntries surfaces seeded warnings on entries without runtime errors', async () => {
    const h = await bootTree()
    await installHello(h, await freshPluginsDir())
    h.tree.warnings.set(NAME, '入口文件与安装记录的哈希不符，已强制禁用')
    const info = h.tree.listEntries().find((e) => e.id === NAME)!
    expect(info.state).toBe('disabled')
    expect(info.error).toContain('哈希不符')
  })

  it('packageEntryId keeps ids loader-safe for plain and scoped names', () => {
    expect(packageEntryId('polaris-plugin-hello')).toBe('polaris-plugin-hello')
    expect(packageEntryId('@zju-real/polaris-plugin-x')).toBe('zju-real--polaris-plugin-x')
    expect(packageEntryId('@a/b').includes(':')).toBe(false)
  })

  it('install record keys share a stable prefix for scanning', () => {
    expect(installRecordKey(NAME)).toBe(`${INSTALL_RECORD_PREFIX}${NAME}`)
  })
})
