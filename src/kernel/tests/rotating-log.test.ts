/* Log files that users are asked to attach to a public issue: they must stay
   small, and they must never contain a key. */
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import { RotatingLog, createSecretScrubber, tailLogFile } from '../src/index.ts'

const dir = mkdtempSync(join(tmpdir(), 'kernel-log-'))
afterAll(() => rmSync(dir, { recursive: true, force: true }))

const plain = (text: string): string => text

describe('RotatingLog', () => {
  it('caps the live file and keeps only the configured generations', () => {
    const path = join(dir, 'cap', 'engine.log')
    const log = new RotatingLog({ path, maxBytes: 2_000, keep: 2, scrub: plain, timestamps: false })
    for (let i = 0; i < 400; i++) log.write(`line ${String(i).padStart(4, '0')} ${'x'.repeat(40)}`)

    expect(statSync(path).size).toBeLessThanOrEqual(2_000)
    expect(existsSync(`${path}.1`)).toBe(true)
    expect(existsSync(`${path}.2`)).toBe(true)
    expect(existsSync(`${path}.3`)).toBe(false)
    expect(statSync(`${path}.1`).size).toBeLessThanOrEqual(2_000)
    // the newest line is in the live file, the oldest ones are gone
    expect(readFileSync(path, 'utf8')).toContain('line 0399')
    const all = [path, `${path}.1`, `${path}.2`].map((p) => readFileSync(p, 'utf8')).join('')
    expect(all).not.toContain('line 0000')
  })

  it('continues an existing file across restarts instead of truncating it', () => {
    const path = join(dir, 'resume', 'main.log')
    new RotatingLog({ path, scrub: plain, timestamps: false }).write('first run')
    new RotatingLog({ path, scrub: plain, timestamps: false }).write('second run')
    expect(readFileSync(path, 'utf8')).toBe('first run\nsecond run\n')
  })

  it('tail reaches into the previous generation when the live file is short', () => {
    const path = join(dir, 'tail', 'engine.log')
    const log = new RotatingLog({ path, maxBytes: 1_200, keep: 3, scrub: plain, timestamps: false })
    for (let i = 0; i < 60; i++) log.write(`row ${i} ${'y'.repeat(30)}`)
    const tail = log.tail(40)
    expect(tail).toHaveLength(40)
    expect(tail.at(-1)).toContain('row 59')
    expect(tail[0]).toContain('row 20')
    expect(tailLogFile(join(dir, 'tail', 'missing.log'), 10)).toEqual([])
  })

  it('timestamps every line and drops blank ones', () => {
    const path = join(dir, 'stamp', 'main.log')
    new RotatingLog({ path, scrub: plain }).write('one\n\n  \ntwo\n')
    const lines = readFileSync(path, 'utf8').trimEnd().split('\n')
    expect(lines).toHaveLength(2)
    expect(lines[0]).toMatch(/^\d{4}-\d\d-\d\dT[\d:.]+Z one$/)
  })

  it('never throws when the file cannot be written', () => {
    // the "directory" is a file: every write fails
    const blocker = join(dir, 'blocker')
    new RotatingLog({ path: blocker, scrub: plain }).write('x')
    const log = new RotatingLog({ path: join(blocker, 'nested', 'a.log'), scrub: plain })
    expect(() => log.write('lost')).not.toThrow()
    expect(() => log.write('lost again')).not.toThrow()
  })
})

describe('secret scrubbing', () => {
  const env = {
    ANTHROPIC_API_KEY: 'sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv',
    GITHUB_TOKEN: 'ghp_0123456789abcdefghij',
    POLARIS_LOCAL_SESSION_SECRET: 'per-launch-session-secret-value',
    DB_PASSWORD: 'correct-horse-battery',
    SHORT_KEY: 'abc',
    HOME: '/Users/someone',
    POLARIS_CLAUDE_MODEL: 'claude-model-name',
  }
  const scrub = createSecretScrubber(() => [env])

  it('replaces values of *_KEY / *_TOKEN / *_SECRET / *_PASSWORD variables wherever they appear', () => {
    const line = `auth ${env.ANTHROPIC_API_KEY} gh=${env.GITHUB_TOKEN} s=${env.POLARIS_LOCAL_SESSION_SECRET} p=${env.DB_PASSWORD}`
    const out = scrub(line)
    for (const value of [env.ANTHROPIC_API_KEY, env.GITHUB_TOKEN, env.POLARIS_LOCAL_SESSION_SECRET, env.DB_PASSWORD]) {
      expect(out).not.toContain(value)
    }
    expect(out).toContain('[redacted:ANTHROPIC_API_KEY]')
    expect(out).toContain('[redacted:GITHUB_TOKEN]')
  })

  it('leaves ordinary values and too-short ones alone', () => {
    expect(scrub('home is /Users/someone, model claude-model-name, abc')).toBe(
      'home is /Users/someone, model claude-model-name, abc',
    )
  })

  it('masks credentials in URLs, bearer tokens and unknown API keys', () => {
    expect(scrub('proxy http://alice:hunter2pass@proxy.example:8080/')).toBe(
      'proxy http://[redacted]@proxy.example:8080/',
    )
    expect(scrub('Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig')).toBe('Authorization: Bearer [redacted]')
    expect(scrub('using sk-ant-zzzzzzzzzzzzzzzzzzzzzzzz now')).toBe('using [redacted:api-key] now')
    expect(scrub('GET /x?api_key=abcdef123456&q=1')).toBe('GET /x?api_key=[redacted]&q=1')
  })

  it('is applied by RotatingLog by default (process environment)', () => {
    const path = join(dir, 'scrub', 'main.log')
    process.env.POLARIS_TEST_ONLY_API_KEY = 'value-that-must-not-reach-disk'
    try {
      new RotatingLog({ path }).write('spawn env has value-that-must-not-reach-disk in it')
    } finally {
      delete process.env.POLARIS_TEST_ONLY_API_KEY
    }
    const text = readFileSync(path, 'utf8')
    expect(text).not.toContain('value-that-must-not-reach-disk')
    expect(text).toContain('[redacted:POLARIS_TEST_ONLY_API_KEY]')
  })
})
