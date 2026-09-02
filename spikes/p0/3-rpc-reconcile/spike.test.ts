// P0 Spike 3 acceptance tests (see issue #574 and the P0 gate report).
import { describe, expect, it } from 'vitest'
import { Reconciler } from './src/reconciler.ts'
import { Sidecar } from './src/supervisor.ts'

const until = async (cond: () => boolean, ms = 5000) => {
  const start = Date.now()
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error('condition not met in time')
    await new Promise((r) => setTimeout(r, 20))
  }
}

function percentile(samples: number[], p: number): number {
  const sorted = [...samples].sort((a, b) => a - b)
  return sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))]!
}

describe('P0 spike 3: JSON-RPC seam + cross-process reconciler', () => {
  it('bidirectional echo x1000 with RTT P95 < 5ms', async () => {
    const sidecar = new Sidecar()
    sidecar.start()
    await sidecar.rpc.call('initialize')
    // Warmup.
    for (let i = 0; i < 50; i++) await sidecar.rpc.call('echo', { i })
    const samples: number[] = []
    for (let i = 0; i < 1000; i++) {
      const t0 = performance.now()
      const res = await sidecar.rpc.call('echo', { seq: i })
      samples.push(performance.now() - t0)
      if ((res as { seq: number }).seq !== i) throw new Error('echo mismatch')
    }
    const p95 = percentile(samples, 95)
    // eslint-disable-next-line no-console
    console.log(`echo RTT p50=${percentile(samples, 50).toFixed(2)}ms p95=${p95.toFixed(2)}ms p99=${percentile(samples, 99).toFixed(2)}ms`)
    await sidecar.stop()
    expect(p95).toBeLessThan(5)
  }, 60_000)

  it('5MB single-line payload survives framing in both directions', async () => {
    const sidecar = new Sidecar()
    sidecar.start()
    const blob = 'x'.repeat(5 * 1024 * 1024 - 16) + 'END-OF-BLOB-MARK'
    const res = (await sidecar.rpc.call('blob.echo', { blob })) as { length: number; tail: string }
    await sidecar.stop()
    expect(res.length).toBe(blob.length)
    expect(res.tail).toBe('END-OF-BLOB-MARK')
  }, 30_000)

  it('add/update/remove each trigger exactly the minimal component effect set', async () => {
    const r = new Reconciler()
    await r.startProcess()

    // Initial tree: two components.
    await r.setDesired([
      { id: 'src:openalex', name: 'openalex-source', config: { key: 'a' } },
      { id: 'parse:grobid', name: 'grobid-parser', config: {} },
    ])
    expect(r.componentEffects().map((e) => `${e.op}:${e.id}`).sort()).toEqual([
      'start:parse:grobid',
      'start:src:openalex',
    ])

    // Add one component: only that one starts.
    let mark = r.effects.length
    await r.setDesired([
      { id: 'src:openalex', name: 'openalex-source', config: { key: 'a' } },
      { id: 'parse:grobid', name: 'grobid-parser', config: {} },
      { id: 'src:pubmed', name: 'pubmed-source', config: {} },
    ])
    expect(r.componentEffects(mark).map((e) => `${e.op}:${e.id}`)).toEqual(['start:src:pubmed'])

    // Update one component's config: only that one updates.
    mark = r.effects.length
    await r.setDesired([
      { id: 'src:openalex', name: 'openalex-source', config: { key: 'b' } },
      { id: 'parse:grobid', name: 'grobid-parser', config: {} },
      { id: 'src:pubmed', name: 'pubmed-source', config: {} },
    ])
    expect(r.componentEffects(mark).map((e) => `${e.op}:${e.id}`)).toEqual(['update:src:openalex'])

    // Remove + disable: exactly two stops, nothing else.
    mark = r.effects.length
    await r.setDesired([
      { id: 'src:openalex', name: 'openalex-source', config: { key: 'b' } },
      { id: 'parse:grobid', name: 'grobid-parser', config: {}, disabled: true },
    ])
    expect(r.componentEffects(mark).map((e) => `${e.op}:${e.id}`).sort()).toEqual([
      'stop:parse:grobid',
      'stop:src:pubmed',
    ])

    await r.sidecar.stop()
  }, 30_000)

  it('kill -9 recovers within 5s, converges, and rejects in-flight calls deterministically', async () => {
    const r = new Reconciler()
    await r.startProcess()
    await r.setDesired([
      { id: 'src:openalex', name: 'openalex-source', config: { key: 'a' } },
      { id: 'src:pubmed', name: 'pubmed-source', config: {} },
    ])
    const beforeConverged = r.effects.filter((e) => e.op === 'converged').length

    // An in-flight call must fail deterministically, not hang.
    const inflight = r.sidecar.rpc.call('echo', { hang: true })
    r.sidecar.child.kill('SIGKILL')
    await expect(inflight).rejects.toThrow('sidecar exited')

    // The supervisor respawns an empty sidecar; the reconciler re-applies the
    // desired tree and converges again.
    await until(() => r.effects.filter((e) => e.op === 'converged').length > beforeConverged)
    expect(r.sidecar.restarts).toBe(1)
    const report = (await r.sidecar.rpc.call('config.report')) as { entries: unknown[] }
    expect(report.entries).toHaveLength(2)
    await r.sidecar.stop()
  }, 30_000)
})
