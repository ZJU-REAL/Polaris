// The kernel must stay startable under bare Node: no electron import may ever
// enter this package (see the header of src/kernel.ts). This test enforces it
// mechanically so no lint stack is needed.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) yield* walk(path)
    else if (/\.(ts|tsx|js|cjs|mjs)$/.test(name)) yield path
  }
}

describe('kernel stays electron-free', () => {
  it('no source file imports electron', () => {
    const offenders: string[] = []
    for (const file of walk(join(import.meta.dirname, '../src'))) {
      const text = readFileSync(file, 'utf8')
      if (/from ['"]electron['"]|require\(['"]electron['"]\)/.test(text)) {
        offenders.push(file)
      }
    }
    expect(offenders).toEqual([])
  })
})
