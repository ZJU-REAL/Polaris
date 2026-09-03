/* storage 插件与 SQLite 存储的单元测试。全部落在 mkdtemp 出来的临时
   目录里，跑完整体删除，不污染仓库。ConfigTreeStore 是接口级契约，
   所以内存实现与 SQLite 实现跑同一套契约用例——两边语义必须一致，
   reconciler 才能在 spike 与正式形态之间无感切换。 */
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import {
  MIGRATIONS,
  MemoryConfigTreeStore,
  PluginMetaStore,
  SqliteConfigTreeStore,
  createKernel,
  migrate,
  openStorage,
  storage,
  type ConfigEntry,
  type ConfigTreeStore,
  type StorageService,
} from '../src/index.ts'

const dir = mkdtempSync(join(tmpdir(), 'kernel-storage-'))
afterAll(() => rmSync(dir, { recursive: true, force: true }))

let seq = 0
const freshPath = (): string => join(dir, `db-${++seq}.sqlite`)

/** 覆盖各字段形态的样例树：嵌套分组、中文、显式 disabled、空 children。 */
const sampleTree = (): ConfigEntry[] => [
  {
    id: 'root',
    name: 'group',
    children: [
      { id: 'root:engine', name: 'legacy-engine', config: { mode: 'docker', 端口: 18080 } },
      { id: 'root:off', name: 'gateway', disabled: true, config: null },
      { id: 'root:empty', name: 'group', children: [] },
    ],
  },
  { id: 'bare', name: '裸插件' },
]

/* ---------- ConfigTreeStore 接口契约：两个实现跑同一套 ---------- */

type StoreFactory = () => { store: ConfigTreeStore; close?: () => void }

const implementations: [string, StoreFactory][] = [
  ['MemoryConfigTreeStore', () => ({ store: new MemoryConfigTreeStore() })],
  [
    'SqliteConfigTreeStore',
    () => {
      const db = openStorage(freshPath())
      migrate(db)
      return { store: new SqliteConfigTreeStore(db), close: () => db.close() }
    },
  ],
]

describe.each(implementations)('ConfigTreeStore contract: %s', (_name, make) => {
  it('starts empty', async () => {
    const { store, close } = make()
    expect(await store.load()).toEqual([])
    close?.()
  })

  it('load returns exactly what was saved, field by field', async () => {
    const { store, close } = make()
    await store.save(sampleTree())
    expect(await store.load()).toEqual(sampleTree())
    close?.()
  })

  it('load returns a detached copy (mutations do not leak back)', async () => {
    const { store, close } = make()
    await store.save(sampleTree())
    const first = await store.load()
    first[0]!.name = 'mutated'
    first[0]!.children!.pop()
    expect(await store.load()).toEqual(sampleTree())
    close?.()
  })

  it('save replaces the whole tree', async () => {
    const { store, close } = make()
    await store.save(sampleTree())
    const next: ConfigEntry[] = [{ id: 'only', name: 'solo', config: { a: [1, 2, 3] } }]
    await store.save(next)
    expect(await store.load()).toEqual(next)
    await store.save([])
    expect(await store.load()).toEqual([])
    close?.()
  })
})

/* ---------- SQLite 专属：跨重开持久化 + 迁移幂等 ---------- */

describe('sqlite storage', () => {
  it('config tree survives close/reopen with identical fields', async () => {
    const path = freshPath()
    const db = openStorage(path)
    migrate(db)
    await new SqliteConfigTreeStore(db).save(sampleTree())
    db.close()

    const reopened = openStorage(path)
    migrate(reopened)
    expect(await new SqliteConfigTreeStore(reopened).load()).toEqual(sampleTree())
    reopened.close()
  })

  it('migrate is idempotent across calls and reopens', () => {
    const path = freshPath()
    const db = openStorage(path)
    migrate(db)
    migrate(db)
    const count = (): number =>
      Number(
        (db.prepare('SELECT COUNT(*) AS n FROM _migrations').get() as { n: number | bigint }).n,
      )
    expect(count()).toBe(MIGRATIONS.length)
    db.close()

    const reopened = openStorage(path)
    migrate(reopened)
    const versions = reopened
      .prepare('SELECT version FROM _migrations ORDER BY version')
      .all() as unknown as { version: number }[]
    expect(versions.map((row) => row.version)).toEqual(MIGRATIONS.map((m) => m.version))
    reopened.close()
  })

  it('plugin meta round-trips JSON values, including CJK and nesting', () => {
    const db = openStorage(freshPath())
    migrate(db)
    const meta = new PluginMetaStore(db)

    expect(meta.get('missing')).toBeUndefined()
    meta.set('文献库', { 名称: '个人库', papers: [{ title: '论文①', score: 0.9 }], 启用: true })
    meta.set('plain', 42)
    expect(meta.get('文献库')).toEqual({
      名称: '个人库',
      papers: [{ title: '论文①', score: 0.9 }],
      启用: true,
    })
    expect(meta.get('plain')).toBe(42)

    // 覆盖写走 upsert 路径
    meta.set('plain', { nested: { deep: ['值'] } })
    expect(meta.get('plain')).toEqual({ nested: { deep: ['值'] } })

    expect(meta.list()).toEqual(['plain', '文献库'])
    expect(meta.delete('plain')).toBe(true)
    expect(meta.delete('plain')).toBe(false)
    expect(meta.list()).toEqual(['文献库'])
    db.close()
  })
})

/* ---------- 插件形态：装载 / 卸载 / 重装 ---------- */

describe('storage plugin', () => {
  it('provides the service, closes the handle on dispose, and reloads', async () => {
    // 故意用不存在的多级子目录：验证 openStorage 的 mkdir -p
    const path = join(dir, 'nested', 'deeper', 'kernel.db')
    const kernel = createKernel({ name: 'storage-test' })
    await kernel.start()

    const fiber = await kernel.ctx.plugin(storage, { path })
    const service = kernel.ctx.get('storage') as StorageService | undefined
    expect(service).toBeTruthy()
    expect(service!.path).toBe(path)
    expect(existsSync(path)).toBe(true)

    service!.pluginMeta.set('probe', { 值: 1 })
    await service!.configTree.save(sampleTree())

    await fiber.dispose()
    // 句柄确实关了：再操作必须抛错，而不是静默写一个死库
    expect(() => service!.db.prepare('SELECT 1')).toThrow()
    expect(kernel.ctx.get('storage')).toBeUndefined()

    // 同一 kernel 里重新装载：migrate 幂等 + 数据还在
    await kernel.ctx.plugin(storage, { path })
    const reloaded = kernel.ctx.get('storage') as StorageService
    expect(reloaded.pluginMeta.get('probe')).toEqual({ 值: 1 })
    expect(await reloaded.configTree.load()).toEqual(sampleTree())

    await kernel.stop()
    expect(() => reloaded.db.prepare('SELECT 1')).toThrow()
  })
})
