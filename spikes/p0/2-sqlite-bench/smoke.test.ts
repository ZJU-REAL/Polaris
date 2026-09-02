// CI smoke: the benchmark pipeline runs end-to-end at a tiny scale.
// Real gate numbers come from local runs recorded in docs/rfcs/p0-spike-report.md.
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { readFileSync, rmSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const here = dirname(fileURLToPath(import.meta.url))

describe('sqlite bench pipeline', () => {
  it('runs end-to-end at small scale', () => {
    const out = join(here, 'smoke-report.json')
    execFileSync('node', [join(here, 'bench.mjs'), '--count', '2000', '--db', '/tmp/spike2-smoke.db', '--json', out], {
      stdio: 'pipe',
      timeout: 120_000,
    })
    const report = JSON.parse(readFileSync(out, 'utf8'))
    rmSync(out)
    rmSync('/tmp/spike2-smoke.db', { force: true })
    expect(report.count).toBe(2000)
    expect(report.vectorKnn.p95).toBeGreaterThan(0)
    expect(report.int8Top20Overlap).toBeGreaterThan(0.8)
  }, 120_000)
})
