// P0 Spike 1 acceptance: the kernel cold-starts the legacy engine (api + arq
// worker + redis as supervised children), drives a full chain over HTTP with
// the deterministic fake LLM, and disposal reclaims every process.
// Skips when docker or the polaris-api-test:local image is unavailable (CI).
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { createKernel } from '@polaris/kernel'
import { applyLegacyEngine, cleanupDocker, dockerAvailable, runningContainers } from './src/legacy-engine.ts'
import { recompilePaper, runChain } from './src/chain.ts'

const backendDir = join(dirname(fileURLToPath(import.meta.url)), '../../../src/backend')
const available = dockerAvailable()

const until = async (cond: () => boolean, ms = 5000) => {
  const start = Date.now()
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error('condition not met in time')
    await new Promise((r) => setTimeout(r, 200))
  }
}

describe.skipIf(!available)('P0 spike 1: kernel drives the legacy engine', () => {
  it('cold start → import → wiki compile → arq index → dispose with no orphans', async () => {
    cleanupDocker()
    const kernel = createKernel({ name: 'spike1' })
    await kernel.start()

    const engine = await applyLegacyEngine(kernel.ctx, { backendDir })
    const result = await runChain(engine.baseUrl)

    expect(result.wikiMarkdown.length).toBeGreaterThan(50)
    expect(result.indexStatus).toBe('built')
    // Queue leg: the arq worker came up against the same redis and registered
    // its task functions (voyage runs are the actual queue consumers). The
    // worker boots after a 5s migration grace period — wait for its banner.
    await until(() => /Starting worker|redis_version/i.test(engine.workerLog()), 30_000)

    // Determinism probe: recompiling again yields byte-identical wiki content.
    const second = await recompilePaper(engine.baseUrl, result.token, result.paperId)
    expect(second).toBe(result.wikiMarkdown)

    await kernel.stop()
    // Effect reclamation: no spike containers may survive disposal.
    const leftover = runningContainers()
    expect(leftover).toEqual([])
  }, 300_000)
})
