import { describe, expect, it } from 'vitest'
import { createKernel, type Context } from '../src/index.ts'

const until = async (cond: () => boolean, ms = 1000) => {
  const start = Date.now()
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error('condition not met in time')
    await new Promise((r) => setTimeout(r, 10))
  }
}

describe('kernel lifecycle', () => {
  it('starts, hosts plugins, and reclaims their effects on stop', async () => {
    const kernel = createKernel({ name: 'test' })
    await kernel.start()
    expect(kernel.started).toBe(true)

    let ticks = 0
    let reclaimed = false
    kernel.ctx.plugin({
      name: 'probe',
      apply(ctx: Context) {
        ctx.effect(() => {
          const timer = setInterval(() => ticks++, 10)
          return () => {
            clearInterval(timer)
            reclaimed = true
          }
        }, 'probe interval')
      },
    })
    await until(() => ticks > 0)

    await kernel.stop()
    expect(kernel.started).toBe(false)
    expect(reclaimed).toBe(true)
    const frozen = ticks
    await new Promise((r) => setTimeout(r, 50))
    expect(ticks).toBe(frozen)
  })

  it('stop is idempotent and start after stop is rejected', async () => {
    const kernel = createKernel()
    await kernel.start()
    await kernel.stop()
    await kernel.stop()
    await expect(kernel.start()).rejects.toThrow('already stopped')
  })
})
