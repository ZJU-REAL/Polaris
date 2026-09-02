// Contract smoke test for the vendored cordis runtime (see vendor/deepseek-cordis).
// Pins the behaviors the Polaris kernel depends on: plugin install/uninstall,
// effect reclamation on dispose, and inject-gated loading driven by services.
import { describe, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'

async function until(cond: () => boolean, ms = 1000): Promise<void> {
  const start = Date.now()
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error('condition not met in time')
    await new Promise((r) => setTimeout(r, 10))
  }
}

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms))

describe('vendored cordis contract', () => {
  it('installs and uninstalls plugins with full effect reclamation', async () => {
    const ctx = new Context()
    let ticks = 0
    let events = 0
    let disposed = false

    const fiber = ctx.plugin({
      name: 'effects-probe',
      apply(ctx: Context) {
        ctx.effect(() => {
          const timer = setInterval(() => ticks++, 20)
          return () => {
            clearInterval(timer)
            disposed = true
          }
        }, 'probe interval')
        ctx.on('probe/event' as any, () => events++)
      },
    })

    await until(() => ticks > 0)
    ctx.emit('probe/event' as any)
    expect(events).toBe(1)

    await fiber.dispose()
    expect(disposed).toBe(true)

    // Timer must be cleared and the listener unregistered after dispose.
    const frozen = ticks
    ctx.emit('probe/event' as any)
    await tick(80)
    expect(ticks).toBe(frozen)
    expect(events).toBe(1)
  })

  it('gates plugin loading on required service injection', async () => {
    const ctx = new Context()
    const seen: unknown[] = []
    let consumerStopped = false

    ctx.plugin({
      name: 'consumer',
      inject: ['answer'],
      apply(ctx: Context) {
        seen.push((ctx as any).answer)
        // An effect disposer runs when the consumer unloads — including the
        // automatic unload triggered by its required service going away.
        ctx.effect(() => () => {
          consumerStopped = true
        }, 'stop probe')
      },
    })

    await tick()
    // Required service missing: the consumer must not have loaded.
    expect(seen).toHaveLength(0)

    const provider = ctx.plugin({
      name: 'provider',
      apply(ctx: Context) {
        ctx.provide('answer', 42)
      },
    })

    await until(() => seen.length === 1)
    expect(seen[0]).toBe(42)

    // Provider goes away: the consumer must be stopped automatically.
    await provider.dispose()
    await until(() => consumerStopped)
  })

  it('validates plugin config through schemastery', () => {
    const Config = Schema.object({
      port: Schema.number().default(8080),
      host: Schema.string().required(),
    })
    expect(new Config({ host: 'localhost' })).toEqual({ host: 'localhost', port: 8080 })
    expect(() => new Config({} as any)).toThrow()
  })
})
