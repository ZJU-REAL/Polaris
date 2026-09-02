import { PassThrough } from 'node:stream'
import { describe, expect, it } from 'vitest'
import { JsonRpcEndpoint } from '../src/index.ts'

/** Wire two endpoints together over in-memory pipes. */
function pair(options?: ConstructorParameters<typeof JsonRpcEndpoint>[2]) {
  const aToB = new PassThrough()
  const bToA = new PassThrough()
  const a = new JsonRpcEndpoint(aToB, bToA, options)
  const b = new JsonRpcEndpoint(bToA, aToB, options)
  return { a, b }
}

describe('line-delimited JSON-RPC endpoint', () => {
  it('pairs requests with responses across the pipe', async () => {
    const { a, b } = pair()
    b.handle('math.add', (params) => {
      const { x, y } = params as { x: number; y: number }
      return x + y
    })
    await expect(a.call('math.add', { x: 2, y: 40 })).resolves.toBe(42)
  })

  it('propagates handler errors as rpc errors', async () => {
    const { a, b } = pair()
    b.handle('boom', () => {
      throw new Error('kaput')
    })
    await expect(a.call('boom')).rejects.toThrow('kaput')
  })

  it('reports unknown methods', async () => {
    const { a } = pair()
    await expect(a.call('no.such.method')).rejects.toThrow('method not found')
  })

  it('delivers notifications without id pairing', async () => {
    const seen: Array<[string, unknown]> = []
    const aToB = new PassThrough()
    const bToA = new PassThrough()
    const a = new JsonRpcEndpoint(aToB, bToA)
    void new JsonRpcEndpoint(bToA, aToB, {
      onNotification: (method, params) => seen.push([method, params]),
    })
    a.notify('progress', { pct: 50 })
    await new Promise((r) => setTimeout(r, 20))
    expect(seen).toEqual([['progress', { pct: 50 }]])
  })

  it('survives garbage lines on the pipe', async () => {
    const { a, b } = pair()
    b.handle('echo', (p) => p)
    // A library logging to stdout must not poison the channel.
    ;(a as unknown as { output: PassThrough }).output.write('not json at all\n')
    await expect(a.call('echo', 'still alive')).resolves.toBe('still alive')
  })

  it('splits and reassembles multiple messages per chunk', async () => {
    const out = new PassThrough()
    const inp = new PassThrough()
    const results: unknown[] = []
    const ep = new JsonRpcEndpoint(out, inp)
    const p1 = ep.call('a').then((r) => results.push(r))
    const p2 = ep.call('b').then((r) => results.push(r))
    // Two responses arrive in one chunk, split mid-line across writes.
    const payload = `${JSON.stringify({ jsonrpc: '2.0', id: 1, result: 'one' })}\n${JSON.stringify({ jsonrpc: '2.0', id: 2, result: 'two' })}\n`
    inp.write(payload.slice(0, 25))
    inp.write(payload.slice(25))
    await Promise.all([p1, p2])
    expect(results).toEqual(['one', 'two'])
  })

  it('rejectAll fails every in-flight call deterministically', async () => {
    const out = new PassThrough()
    const inp = new PassThrough()
    const ep = new JsonRpcEndpoint(out, inp)
    const p = ep.call('never.answered')
    ep.rejectAll('peer died')
    await expect(p).rejects.toThrow('peer died')
  })

  it('times out calls that never get a response', async () => {
    const out = new PassThrough()
    const inp = new PassThrough()
    const ep = new JsonRpcEndpoint(out, inp, { timeoutMs: 50 })
    await expect(ep.call('slow')).rejects.toThrow('timed out')
  })
})
