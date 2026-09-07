/* plugins.* IPC 方法族的 kernel 侧支撑（#705）：SqliteTree 的
   listEntries / validateConfig / exportTree / importTree。桌面 IPC 层只是
   薄壳，语义全在这里——所以状态映射、schema 校验、恶意载荷拒绝与
   last-good 回滚都在 kernel 面测完，desktop smoke 只验接线。 */
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import Schema from '@deepseek-ai/schemastery'
import type { Context, Fiber } from '@deepseek-ai/cordis'
import {
  CONFIG_TREE_LAST_GOOD_KEY,
  Loader,
  MemoryConfigTreeStore,
  PluginMetaStore,
  SqliteTree,
  createKernel,
  describeEntryState,
  migrate,
  openStorage,
  registerBuiltins,
  type ConfigEntry,
  type ConfigTreeStore,
  type Kernel,
} from '../src/index.ts'

const dir = mkdtempSync(join(tmpdir(), 'kernel-plugins-manage-'))
afterAll(() => rmSync(dir, { recursive: true, force: true }))

let seq = 0
const freshPath = (): string => join(dir, `db-${++seq}.sqlite`)

/**
 * 起一个装好 Loader + 内置表的 kernel，另注册三个测试插件：
 * - cordis:fake     无 Config，装载即成功（active 状态样本）
 * - cordis:confy    带 schemastery Config（校验正反用例）
 * - cordis:stuck    声明依赖不存在的服务（fiber 停在 PENDING 的样本）
 */
async function bootKernel(): Promise<Kernel> {
  const kernel = createKernel({ name: 'plugins-manage-test' })
  await kernel.start()
  await kernel.ctx.plugin(Loader)
  const loader = kernel.ctx.loader
  registerBuiltins(loader)
  loader.builtins['fake'] = { name: 'fake', apply(): void {} }
  loader.builtins['confy'] = {
    name: 'confy',
    Config: Schema.object({
      port: Schema.number().required(),
      host: Schema.string().default('127.0.0.1'),
    }),
    apply(_ctx: Context, _config: unknown): void {},
  }
  loader.builtins['stuck'] = {
    name: 'stuck',
    inject: ['no-such-service'],
    apply(): void {},
  }
  return kernel
}

async function mountTree(kernel: Kernel, store: ConfigTreeStore): Promise<SqliteTree> {
  await kernel.ctx.plugin(SqliteTree, { store })
  return kernel.ctx.get('configTree') as SqliteTree
}

describe('listEntries', () => {
  it('maps entry runtime to state and surfaces config for plain entries only', async () => {
    const store = new MemoryConfigTreeStore()
    await store.save([
      { id: 'up', name: 'cordis:fake', config: { a: 1 } },
      { id: 'off', name: 'cordis:fake', disabled: true, config: { b: 2 } },
      { id: 'wait', name: 'cordis:stuck' },
      { id: 'grp', name: 'cordis:group', children: [{ id: 'child', name: 'cordis:fake' }] },
    ])
    const kernel = await bootKernel()
    const tree = await mountTree(kernel, store)

    const byId = new Map(tree.listEntries().map((info) => [info.id, info]))
    expect([...byId.keys()].sort()).toEqual(['child', 'grp', 'off', 'up', 'wait'])

    expect(byId.get('up')).toMatchObject({
      name: 'cordis:fake',
      disabled: false,
      state: 'active',
      config: { a: 1 },
    })
    expect(byId.get('off')).toMatchObject({ disabled: true, state: 'disabled', config: { b: 2 } })
    // 依赖不存在的服务：fiber 建了但等在 PENDING
    expect(byId.get('wait')?.state).toBe('pending')
    // 组条目的 config 是子条目列表（内部表示），不作为插件配置暴露
    expect(byId.get('grp')?.config).toBeUndefined()
    expect(byId.get('child')?.state).toBe('active')
    await kernel.stop()
  })

  it('describeEntryState reports error with the fiber failure message', () => {
    // FAILED 态难以从公开面稳定构造（装载失败会整树回滚），状态推导函数
    // 单独可测：用与真实 Fiber 同形的桩子覆盖 error 分支
    const failed = { uid: 1, store: undefined, _error: new Error('boom') } as unknown as Fiber
    expect(describeEntryState(failed, false)).toEqual({ state: 'error', error: 'boom' })
    const active = { uid: 1, store: {} } as unknown as Fiber
    expect(describeEntryState(active, false)).toEqual({ state: 'active' })
    expect(describeEntryState(undefined, true)).toEqual({ state: 'disabled' })
    expect(describeEntryState(undefined, false)).toEqual({ state: 'pending' })
  })
})

describe('validateConfig', () => {
  it('passes a valid config and one without a schema', async () => {
    const kernel = await bootKernel()
    const tree = await mountTree(kernel, new MemoryConfigTreeStore())
    expect(tree.validateConfig('cordis:confy', { port: 3000 })).toEqual({ ok: true, errors: [] })
    // 无 Config 的插件与非内置 specifier（拿不到 schema）都视为通过
    expect(tree.validateConfig('cordis:fake', { anything: true }).ok).toBe(true)
    expect(tree.validateConfig('some-npm-plugin', { anything: true }).ok).toBe(true)
    await kernel.stop()
  })

  it('collects schema errors, unknown builtins and __jsExpr as data', async () => {
    const kernel = await bootKernel()
    const tree = await mountTree(kernel, new MemoryConfigTreeStore())

    const badType = tree.validateConfig('cordis:confy', { port: 'not-a-number' })
    expect(badType.ok).toBe(false)
    expect(badType.errors.length).toBeGreaterThan(0)

    const missing = tree.validateConfig('cordis:confy', {})
    expect(missing.ok).toBe(false)

    const unknown = tree.validateConfig('cordis:no-such-plugin', {})
    expect(unknown.ok).toBe(false)
    expect(unknown.errors[0]!.message).toMatch(/no-such-plugin/)

    // D5：__jsExpr 在校验层同样封死，且错误里带具体路径
    const evil = tree.validateConfig('cordis:confy', {
      port: 3000,
      nested: { __jsExpr: 'process.exit(1)' },
    })
    expect(evil.ok).toBe(false)
    expect(evil.errors.some((e) => e.message.includes('__jsExpr'))).toBe(true)
    await kernel.stop()
  })
})

describe('exportTree / importTree', () => {
  const seedEntries: ConfigEntry[] = [
    { id: 'up', name: 'cordis:fake', config: { a: 1 } },
    { id: 'off', name: 'cordis:fake', disabled: true },
    { id: 'grp', name: 'cordis:group', children: [{ id: 'child', name: 'cordis:fake' }] },
  ]

  /** 真 SQLite 的 PluginMetaStore：last-good 的持久语义要在真实现上验。 */
  function makeMeta(): { meta: PluginMetaStore; close: () => void } {
    const db = openStorage(freshPath())
    migrate(db)
    return { meta: new PluginMetaStore(db), close: () => db.close() }
  }

  it('roundtrips: export from one tree, import into an empty one', async () => {
    const storeA = new MemoryConfigTreeStore()
    await storeA.save(seedEntries)
    const kernelA = await bootKernel()
    const treeA = await mountTree(kernelA, storeA)
    const exported = treeA.exportTree()
    expect(exported).toEqual(seedEntries)
    await kernelA.stop()

    const { meta, close } = makeMeta()
    const storeB = new MemoryConfigTreeStore()
    const kernelB = await bootKernel()
    const treeB = await mountTree(kernelB, storeB)
    await treeB.importTree(exported, meta)

    // 导入即装载：active/disabled 状态与源树一致，且已 flush 落库
    const byId = new Map(treeB.listEntries().map((info) => [info.id, info]))
    expect(byId.get('up')?.state).toBe('active')
    expect(byId.get('off')?.state).toBe('disabled')
    expect(byId.get('child')?.state).toBe('active')
    expect(await storeB.load()).toEqual(seedEntries)
    // 导入前的树是空的，last-good 记录的就是空树
    expect(meta.get(CONFIG_TREE_LAST_GOOD_KEY)).toEqual([])
    await kernelB.stop()
    close()
  })

  it('replaces the whole tree and snapshots the previous one as last-good', async () => {
    const store = new MemoryConfigTreeStore()
    await store.save(seedEntries)
    const kernel = await bootKernel()
    const tree = await mountTree(kernel, store)
    const { meta, close } = makeMeta()

    const next: ConfigEntry[] = [{ id: 'solo', name: 'cordis:fake', config: { fresh: true } }]
    await tree.importTree(next, meta)

    expect(tree.listEntries().map((info) => info.id)).toEqual(['solo'])
    expect(await store.load()).toEqual(next)
    expect(meta.get(CONFIG_TREE_LAST_GOOD_KEY)).toEqual(seedEntries)
    await kernel.stop()
    close()
  })

  it('rejects malicious payloads before touching the live tree', async () => {
    const store = new MemoryConfigTreeStore()
    await store.save(seedEntries)
    const kernel = await bootKernel()
    const tree = await mountTree(kernel, store)
    const { meta, close } = makeMeta()

    const cases: [string, ConfigEntry[], RegExp][] = [
      [
        '__jsExpr',
        [{ id: 'evil', name: 'cordis:fake', config: { x: { __jsExpr: 'process.exit(1)' } } }],
        /__jsExpr/,
      ],
      [
        'loader extension field',
        [{ id: 'evil', name: 'cordis:fake', inject: ['storage'] } as unknown as ConfigEntry],
        /inject/,
      ],
      [
        'duplicate id',
        [
          { id: 'dup', name: 'cordis:fake' },
          { id: 'dup', name: 'cordis:fake' },
        ],
        /重复/,
      ],
      ['reserved separator in id', [{ id: 'a:b', name: 'cordis:fake' }], /:/],
    ]
    for (const [, payload, pattern] of cases) {
      await expect(tree.importTree(payload, meta)).rejects.toThrow(pattern)
    }

    // 校验在动树之前：原树原样活着、last-good 没被写（快照晚于校验）
    expect(new Map(tree.listEntries().map((i) => [i.id, i.state])).get('up')).toBe('active')
    expect(tree.exportTree()).toEqual(seedEntries)
    expect(meta.get(CONFIG_TREE_LAST_GOOD_KEY)).toBeUndefined()
    await kernel.stop()
    close()
  })

  it('rolls back to last-good when a valid-looking payload fails to load', async () => {
    const store = new MemoryConfigTreeStore()
    await store.save(seedEntries)
    const kernel = await bootKernel()
    const tree = await mountTree(kernel, store)
    const { meta, close } = makeMeta()

    // 形状合法但装不上：未知内置插件在 import 阶段抛错（启用条目才触发）
    const payload: ConfigEntry[] = [
      { id: 'ok', name: 'cordis:fake' },
      { id: 'broken', name: 'cordis:no-such-plugin' },
    ]
    await expect(tree.importTree(payload, meta)).rejects.toThrow(/no-such-plugin/)

    // 快照已落 KV（替换动手之前），内存树滚回旧集合、fiber 活着，库里也是旧树
    expect(meta.get(CONFIG_TREE_LAST_GOOD_KEY)).toEqual(seedEntries)
    const byId = new Map(tree.listEntries().map((info) => [info.id, info]))
    expect([...byId.keys()].sort()).toEqual(['child', 'grp', 'off', 'up'])
    expect(byId.get('up')?.state).toBe('active')
    expect(await store.load()).toEqual(seedEntries)
    await kernel.stop()
    close()
  })
})
