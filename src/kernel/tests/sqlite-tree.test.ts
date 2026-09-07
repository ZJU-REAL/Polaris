/* SqliteTree 的单元测试（#699）。ConfigTreeStore 是接口级契约，内存
   实现与 SQLite 实现跑同一套关键路径（describe.each，同 storage.test），
   保证「测试用内存、生产用 SQLite」不会踩语义差。装载/热更/回滚/
   启停全部走 loader 的 Entry.update 分档策略，测试只从公开面断言。 */
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import type { Context } from '@deepseek-ai/cordis'
import type { EntryOptions } from '@deepseek-ai/cordis-plugin-loader'
import {
  Loader,
  MemoryConfigTreeStore,
  SqliteConfigTreeStore,
  SqliteTree,
  createKernel,
  migrate,
  openStorage,
  registerBuiltins,
  type ConfigEntry,
  type ConfigTreeStore,
  type Kernel,
} from '../src/index.ts'

const dir = mkdtempSync(join(tmpdir(), 'kernel-sqlite-tree-'))
afterAll(() => rmSync(dir, { recursive: true, force: true }))

let seq = 0
const freshPath = (): string => join(dir, `db-${++seq}.sqlite`)

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

/** 起一个装好 Loader + 内置表的 kernel，并注册记副作用的假插件 cordis:fake。 */
async function bootKernel(): Promise<{
  kernel: Kernel
  calls: unknown[]
  imports: () => number
}> {
  const kernel = createKernel({ name: 'sqlite-tree-test' })
  await kernel.start()
  await kernel.ctx.plugin(Loader)
  const loader = kernel.ctx.loader
  registerBuiltins(loader)

  const calls: unknown[] = []
  let imports = 0
  const fakePlugin = {
    name: 'fake',
    apply(_ctx: Context, config: unknown): void {
      calls.push(config)
    },
  }
  // getter 计数：只有 EntryTree.import 真的查表才 +1，`in` 判断不触发，
  // 借此区分「重新装载（重 import）」与「就地热更（不 import）」
  Object.defineProperty(loader.builtins, 'fake', {
    configurable: true,
    enumerable: true,
    get() {
      imports += 1
      return fakePlugin
    },
  })
  return { kernel, calls, imports: () => imports }
}

async function mountTree(kernel: Kernel, store: ConfigTreeStore): Promise<SqliteTree> {
  await kernel.ctx.plugin(SqliteTree, { store })
  return kernel.ctx.get('configTree') as SqliteTree
}

const entryIds = (tree: SqliteTree): string[] =>
  [...tree.entries()].map((entry) => entry.options.id).sort()

describe.each(implementations)('SqliteTree over %s', (_name, make) => {
  it('roundtrip: seed → mutate → flush → reload restores the same set', async () => {
    const { store, close } = make()
    await store.save([
      { id: 'probe', name: 'cordis:desktop-probe' },
      {
        id: 'grp',
        name: 'cordis:group',
        children: [{ id: 'fake1', name: 'cordis:fake', config: { a: 1 } }],
      },
      { id: 'off', name: 'cordis:fake', disabled: true, config: { c: 3 } },
    ])

    const { kernel } = await bootKernel()
    const tree = await mountTree(kernel, store)
    expect(entryIds(tree)).toEqual(['fake1', 'grp', 'off', 'probe'])
    expect(tree.resolve('fake1').fiber).toBeTruthy()
    // disabled 条目：留在树里但不实例化
    expect(tree.resolve('off').fiber).toBeUndefined()

    const newId = await tree.create({ name: 'cordis:fake', config: { b: 2 } }, 'grp')
    await tree.update('fake1', { config: { a: 9 } })
    await tree.remove('probe')
    await tree.flush()

    const expected: ConfigEntry[] = [
      {
        id: 'grp',
        name: 'cordis:group',
        children: [
          { id: 'fake1', name: 'cordis:fake', config: { a: 9 } },
          { id: newId, name: 'cordis:fake', config: { b: 2 } },
        ],
      },
      { id: 'off', name: 'cordis:fake', disabled: true, config: { c: 3 } },
    ]
    expect(await store.load()).toEqual(expected)
    await kernel.stop()

    // 新 kernel + 新 SqliteTree 重载：恢复同一集合，store 内容不漂移
    const second = await bootKernel()
    const tree2 = await mountTree(second.kernel, store)
    expect(entryIds(tree2)).toEqual(['fake1', 'grp', newId, 'off'].sort())
    expect(tree2.resolve(newId).fiber).toBeTruthy()
    await tree2.flush()
    expect(await store.load()).toEqual(expected)
    await second.kernel.stop()
    close?.()
  })

  it('config-only update hot-swaps in place: same fiber, no re-import', async () => {
    const { store, close } = make()
    await store.save([{ id: 'f1', name: 'cordis:fake', config: { a: 1 } }])
    const { kernel, calls, imports } = await bootKernel()
    const tree = await mountTree(kernel, store)
    expect(imports()).toBe(1)
    expect(calls).toEqual([{ a: 1 }])

    const entry = tree.resolve('f1')
    const fiber = entry.fiber
    expect(fiber).toBeTruthy()

    // 深等的 config：Entry.update 短路，apply 完全不重跑
    await tree.update('f1', { config: { a: 1 } })
    expect(calls).toHaveLength(1)

    // 真变更：fiber 同引用、不重新 import（不是「重装」），apply 以新
    // 配置就地重启——这是 cordis fiber.update 的既定热更语义
    await tree.update('f1', { config: { a: 2 } })
    expect(entry.fiber).toBe(fiber)
    expect(imports()).toBe(1)
    expect(calls.at(-1)).toEqual({ a: 2 })

    await tree.flush()
    expect(await store.load()).toEqual([{ id: 'f1', name: 'cordis:fake', config: { a: 2 } }])
    await kernel.stop()
    close?.()
  })

  it('bad package rollback: unknown specifier throws before touching the live fiber', async () => {
    const { store, close } = make()
    await store.save([{ id: 'f1', name: 'cordis:fake', config: { a: 1 } }])
    const { kernel, calls } = await bootKernel()
    const tree = await mountTree(kernel, store)
    const entry = tree.resolve('f1')
    const fiber = entry.fiber

    await expect(
      tree.update('f1', { name: 'cordis:no-such-plugin' } as unknown as Omit<EntryOptions, 'id' | 'name'>),
    ).rejects.toThrow(/no-such-plugin/)

    // 原 fiber 原样活着：引用未变、apply 未重跑、options 已回滚
    expect(entry.fiber).toBe(fiber)
    expect(entry.options.name).toBe('cordis:fake')
    expect(calls).toHaveLength(1)
    await tree.flush()
    expect(await store.load()).toEqual([{ id: 'f1', name: 'cordis:fake', config: { a: 1 } }])
    await kernel.stop()
    close?.()
  })

  it('disabled toggle disposes and rebuilds the fiber', async () => {
    const { store, close } = make()
    await store.save([{ id: 'f1', name: 'cordis:fake', config: { a: 1 } }])
    const { kernel, calls } = await bootKernel()
    const tree = await mountTree(kernel, store)
    const entry = tree.resolve('f1')
    const fiber = entry.fiber
    expect(fiber).toBeTruthy()

    await tree.update('f1', { disabled: true })
    expect(entry.fiber).toBeUndefined()
    await tree.flush()
    expect(await store.load()).toEqual([
      { id: 'f1', name: 'cordis:fake', disabled: true, config: { a: 1 } },
    ])

    // disabled: null 表示删除该键（loader 的 isNullable 语义）→ 重建 fiber
    await tree.update('f1', { disabled: null })
    expect(entry.fiber).toBeTruthy()
    expect(entry.fiber).not.toBe(fiber)
    expect(calls).toHaveLength(2)
    await tree.flush()
    expect(await store.load()).toEqual([{ id: 'f1', name: 'cordis:fake', config: { a: 1 } }])
    await kernel.stop()
    close?.()
  })

  it('rejects __jsExpr nodes at any depth with the offending path', async () => {
    const { store, close } = make()
    await store.save([
      {
        id: 'evil',
        name: 'cordis:fake',
        config: { outer: { inner: { __jsExpr: 'process.exit(1)' } } },
      } as ConfigEntry,
    ])
    const { kernel } = await bootKernel()
    await expect(kernel.ctx.plugin(SqliteTree, { store })).rejects.toThrow(
      /evil\.config\.outer\.inner.*__jsExpr/,
    )
    await kernel.stop()
    close?.()
  })
})

/* inject/isolate/intercept 拒载只在内存 store 上测：SQLite 的表结构
   （id/name/config/disabled 四列）天然装不下 loader 扩展字段，这道
   防线针对的是内存树与后续 importTree（PR-3）之类的整树输入路径。 */
describe('SqliteTree rejection of loader extension fields', () => {
  it('rejects an inject field with the offending path', async () => {
    const store = new MemoryConfigTreeStore()
    await store.save([
      {
        id: 'grp',
        name: 'cordis:group',
        children: [{ id: 'bad', name: 'cordis:fake', inject: ['storage'] }],
      } as unknown as ConfigEntry,
    ])
    const { kernel } = await bootKernel()
    await expect(kernel.ctx.plugin(SqliteTree, { store })).rejects.toThrow(/grp\.bad.*inject/)
    await kernel.stop()
  })
})
