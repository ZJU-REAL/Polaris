/* The plugin host's guarantees about the stored config tree:
   - the engine's launch-time config never reaches it (it used to: any tree
     write persisted the injected command line with absolute paths, and the
     next launch started the engine from that stale spec);
   - rows left behind by older versions are cleaned up instead of making the
     whole tree unloadable;
   - a tree that still cannot be loaded is backed up and reset. */
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import {
  CONFIG_TREE_BACKUP_PREFIX,
  createPluginHost,
  dropUnknownBuiltins,
  mountEngine,
  type ConfigEntry,
  type LegacyEngineService,
} from '../src/index.ts'

const root = mkdtempSync(join(tmpdir(), 'kernel-host-'))
afterAll(() => rmSync(root, { recursive: true, force: true }))

let seq = 0
const freshRoot = (): string => join(root, `data-${++seq}`)
const quiet = { warn: () => undefined, error: () => undefined }

function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const server = createServer()
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as { port: number }
      server.close(() => resolve(port))
    })
  })
}

function fakeEngine(port: number): string[] {
  const script = [
    "const http = require('node:http');",
    'http.createServer((req, res) => {',
    "  res.setHeader('content-type', 'application/json');",
    "  res.end(JSON.stringify({ status: 'ok', version: 'fake', instance_id: process.env.POLARIS_INSTANCE_ID || '' }));",
    `}).listen(${port}, '127.0.0.1');`,
  ].join('\n')
  return [process.execPath, '-e', script]
}

/** What older versions seeded, including an engine row that leaked its launch config. */
function legacyRows(dataRoot: string): ConfigEntry[] {
  return [
    { id: 'desktop-probe', name: 'cordis:desktop-probe', config: { smokeTouched: true } },
    { id: 'sources', name: 'cordis:sources', disabled: true },
    {
      id: 'legacy-engine',
      name: 'cordis:legacy-engine',
      config: {
        mode: 'command',
        command: ['/Applications/Polaris.app/old/venv/bin/python', '-c', 'import uvicorn'],
        port: 18080,
        secretsFile: join(dataRoot, 'engine-secrets.json'),
      },
    },
  ]
}

describe('engine mounted outside the config tree', () => {
  it('keeps the engine spec out of the stored tree and the export, whatever else is written', async () => {
    const dataRoot = freshRoot()
    const port = await freePort()
    const host = await createPluginHost({ name: 't', dataRoot, seedEntries: [], strict: false, log: quiet })
    const tree = host.configTree!
    expect(tree).toBeTruthy()

    await mountEngine(host.kernel, {
      mode: 'command',
      command: fakeEngine(port),
      port,
      pidFile: join(dataRoot, 'engine.pid.json'),
      secretsFile: join(dataRoot, 'engine-secrets.json'),
    })
    const legacy = host.kernel.ctx.get('legacy') as LegacyEngineService
    expect(legacy.baseUrl).toBe(`http://127.0.0.1:${port}`)

    // any tree write used to serialise the engine row along with everything else
    host.kernel.ctx.loader.builtins['fake'] = { name: 'fake', apply(): void {} }
    await tree.create({ id: 'mine', name: 'cordis:fake', config: { a: 1 } } as never)
    await tree.update('mine', { disabled: true })
    await tree.flush()

    const stored = JSON.stringify(await host.store.load())
    const exported = JSON.stringify(tree.exportTree())
    for (const text of [stored, exported]) {
      expect(text).not.toContain('legacy-engine')
      expect(text).not.toContain(process.execPath)
      expect(text).not.toContain('engine-secrets')
    }
    expect(tree.listEntries().map((entry) => entry.id)).toEqual(['mine'])

    await host.kernel.stop()
    expect(host.kernel.ctx.get('legacy')).toBeUndefined()
  }, 30_000)
})

describe('stored trees from older versions', () => {
  it('dropUnknownBuiltins removes only cordis: rows that are not registered, at any depth', () => {
    const entries: ConfigEntry[] = [
      { id: 'a', name: 'cordis:gone' },
      { id: 'b', name: 'polaris-plugin-hello', disabled: true },
      {
        id: 'g',
        name: 'cordis:group',
        children: [
          { id: 'c', name: 'cordis:legacy-engine', config: { mode: 'command' } },
          { id: 'd', name: 'file:///somewhere/plugin.mjs' },
        ],
      },
    ]
    const out = dropUnknownBuiltins(entries, new Set(['group']))
    expect(out.dropped).toEqual(['a', 'c'])
    expect(out.entries).toEqual([
      { id: 'b', name: 'polaris-plugin-hello', disabled: true },
      { id: 'g', name: 'cordis:group', children: [{ id: 'd', name: 'file:///somewhere/plugin.mjs' }] },
    ])
    // the input is not modified
    expect(entries).toHaveLength(3)
  })

  it('drops the retired built-in rows (leaked engine spec included), keeps the rest, and leaves a backup', async () => {
    const dataRoot = freshRoot()
    const first = await createPluginHost({ name: 't', dataRoot, seedEntries: [], strict: false, log: quiet })
    await first.store.save([
      ...legacyRows(dataRoot),
      { id: 'polaris-plugin-kept', name: 'polaris-plugin-kept', disabled: true, config: { keep: 'me' } },
    ])
    await first.kernel.stop()

    const warnings: string[] = []
    const host = await createPluginHost({
      name: 't',
      dataRoot,
      seedEntries: [],
      strict: false,
      log: { warn: (m) => warnings.push(m), error: () => undefined },
    })
    try {
      expect(host.configTree).toBeTruthy()
      expect(host.kernel.ctx.get('legacy')).toBeUndefined()
      expect(await host.store.load()).toEqual([
        { id: 'polaris-plugin-kept', name: 'polaris-plugin-kept', disabled: true, config: { keep: 'me' } },
      ])
      expect(host.repairs).toHaveLength(1)
      const repair = host.repairs[0]!
      expect(repair.kind).toBe('dropped-builtins')
      expect(repair.dropped).toEqual(['desktop-probe', 'sources', 'legacy-engine'])
      expect(warnings.join('\n')).toContain('legacy-engine')

      // the pre-cleanup tree is recoverable from both places
      expect(repair.backupKey?.startsWith(CONFIG_TREE_BACKUP_PREFIX)).toBe(true)
      const kv = host.pluginMeta!.get(repair.backupKey!) as { entries: ConfigEntry[] }
      expect(kv.entries.map((entry) => entry.id)).toContain('legacy-engine')
      const file = JSON.parse(readFileSync(repair.backupFile!, 'utf8')) as { entries: ConfigEntry[] }
      expect(file.entries).toHaveLength(4)
    } finally {
      await host.kernel.stop()
    }

    // idempotent: the next launch has nothing left to clean
    const again = await createPluginHost({ name: 't', dataRoot, seedEntries: [], strict: false, log: quiet })
    expect(again.repairs).toEqual([])
    expect(again.configTree).toBeTruthy()
    await again.kernel.stop()
  })

  it('backs up and resets a tree that cannot be loaded, instead of running without one forever', async () => {
    const dataRoot = freshRoot()
    const first = await createPluginHost({ name: 't', dataRoot, seedEntries: [], strict: false, log: quiet })
    const missing = join(dataRoot, 'plugins', 'gone', 'index.mjs')
    await first.store.save([
      { id: 'broken', name: `file://${missing}`, config: { precious: true } },
      { id: 'fine', name: 'polaris-plugin-fine', disabled: true },
    ])
    await first.kernel.stop()

    const host = await createPluginHost({ name: 't', dataRoot, seedEntries: [], strict: false, log: quiet })
    try {
      expect(host.configTree).toBeTruthy()
      expect(await host.store.load()).toEqual([])
      expect(host.repairs.map((repair) => repair.kind)).toEqual(['reset'])
      const repair = host.repairs[0]!
      expect(repair.reason).toBeTruthy()
      expect(existsSync(repair.backupFile!)).toBe(true)
      const saved = JSON.parse(readFileSync(repair.backupFile!, 'utf8')) as { entries: ConfigEntry[]; reason: string }
      expect(saved.entries.map((entry) => entry.id)).toEqual(['broken', 'fine'])
      expect(saved.entries[0]!.config).toEqual({ precious: true })

      // the reset tree is a working one: it accepts writes again
      host.kernel.ctx.loader.builtins['fake'] = { name: 'fake', apply(): void {} }
      await host.configTree!.create({ id: 'again', name: 'cordis:fake' } as never)
      await host.configTree!.flush()
      expect((await host.store.load()).map((entry) => entry.id)).toEqual(['again'])
    } finally {
      await host.kernel.stop()
    }
  })
})
