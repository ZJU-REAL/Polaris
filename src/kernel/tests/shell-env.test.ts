// Login-shell env pickup: the parser is pure and the shell is injected, so
// none of the unit cases runs the developer's real shell. The last block
// drives the default runner against throwaway fake "shells" (POSIX only).
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  ENGINE_LLM_ENV,
  applyShellEnv,
  parseShellEnv,
  resolveShellEnv,
  runShell,
  type ShellRunner,
} from '../src/index.ts'

const M = { begin: '__B__', end: '__E__' }

/** A runner that answers with a fixed dump and records how it was called. */
function fakeRunner(stdout: string | null): { run: ShellRunner; calls: Array<{ shell: string; args: string[] }> } {
  const calls: Array<{ shell: string; args: string[] }> = []
  return {
    calls,
    run: async (shell, args) => {
      calls.push({ shell, args })
      return stdout
    },
  }
}

describe('parseShellEnv', () => {
  it('picks only the allowlisted names', () => {
    const out = `${M.begin}PATH=/usr/bin\nANTHROPIC_API_KEY=sk-a\nAWS_SECRET_ACCESS_KEY=nope\nPOLARIS_CLAUDE_MODEL=m1\nPOLARIS_LLM_FAKE_FALLBACK=1\n${M.end}`
    expect(parseShellEnv(out, M)).toEqual({ ANTHROPIC_API_KEY: 'sk-a', POLARIS_CLAUDE_MODEL: 'm1' })
  })

  it('ignores banner noise outside the markers, even when it looks like a wanted variable', () => {
    const out = [
      'Welcome to my shell!',
      'ANTHROPIC_API_KEY=from-banner',
      `${M.begin}LITELLM_API_KEY=real`,
      `${M.end}`,
      'ANTHROPIC_BASE_URL=http://after-the-end',
    ].join('\n')
    expect(parseShellEnv(out, M)).toEqual({ LITELLM_API_KEY: 'real' })
  })

  it('keeps everything after the first "=" in a value', () => {
    const out = `${M.begin}ANTHROPIC_BASE_URL=http://10.0.0.5:8010/v1?a=b&c=d\nANTHROPIC_API_KEY=abc==\n${M.end}`
    expect(parseShellEnv(out, M)).toEqual({
      ANTHROPIC_BASE_URL: 'http://10.0.0.5:8010/v1?a=b&c=d',
      ANTHROPIC_API_KEY: 'abc==',
    })
  })

  it('reads the NUL-separated form and is not fooled by other variables spanning lines', () => {
    const out = `${M.begin}${['FUNC=() {\nANTHROPIC_API_KEY=inside-a-function\n}', 'ANTHROPIC_API_KEY=real', 'HOME=/h'].join('\0')}\0${M.end}`
    expect(parseShellEnv(out, M)).toEqual({ ANTHROPIC_API_KEY: 'real' })
  })

  it('skips multi-line leftovers and empty values in the newline form', () => {
    const out = `${M.begin}FUNC=() {\n  echo hi\n}\n=odd\nno-equals-here\nLITELLM_API_BASE=\nLITELLM_API_KEY=k\n${M.end}`
    expect(parseShellEnv(out, M)).toEqual({ LITELLM_API_KEY: 'k' })
  })

  it('returns nothing when a marker is missing (shell died halfway)', () => {
    expect(parseShellEnv('ANTHROPIC_API_KEY=x\n', M)).toEqual({})
    expect(parseShellEnv(`${M.begin}ANTHROPIC_API_KEY=x\n`, M)).toEqual({})
  })
})

describe('resolveShellEnv / applyShellEnv', () => {
  const dump = `motd\n${M.begin}ANTHROPIC_API_KEY=from-shell\nANTHROPIC_BASE_URL=http://10.1.2.3:8010\nPATH=/x\n${M.end}\n`

  it('runs the login shell interactively and sets only missing allowlisted names', async () => {
    const env: NodeJS.ProcessEnv = { SHELL: '/bin/myshell', PATH: '/usr/bin' }
    const { run, calls } = fakeRunner(dump)
    const applied = await applyShellEnv({ env, platform: 'darwin', run, markers: M })
    expect(applied.sort()).toEqual(['ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL'])
    expect(env.ANTHROPIC_API_KEY).toBe('from-shell')
    expect(env.ANTHROPIC_BASE_URL).toBe('http://10.1.2.3:8010')
    expect(env.PATH).toBe('/usr/bin') // not on the list: untouched
    expect(calls).toHaveLength(1)
    expect(calls[0]!.shell).toBe('/bin/myshell')
    expect(calls[0]!.args[0]).toBe('-ilc')
    expect(calls[0]!.args[1]).toContain(M.begin)
    expect(calls[0]!.args[1]).toContain(M.end)
  })

  it('never overrides a variable that is already set; an empty one counts as unset', async () => {
    const env: NodeJS.ProcessEnv = { SHELL: '/bin/zsh', ANTHROPIC_API_KEY: 'mine', ANTHROPIC_BASE_URL: '' }
    const applied = await applyShellEnv({ env, platform: 'linux', run: fakeRunner(dump).run, markers: M })
    expect(applied).toEqual(['ANTHROPIC_BASE_URL'])
    expect(env.ANTHROPIC_API_KEY).toBe('mine')
    expect(env.ANTHROPIC_BASE_URL).toBe('http://10.1.2.3:8010')
  })

  it('falls back to zsh on macOS and bash on Linux when SHELL is unset', async () => {
    const mac = fakeRunner(dump)
    await resolveShellEnv({ env: {}, platform: 'darwin', run: mac.run, markers: M })
    expect(mac.calls[0]!.shell).toBe('/bin/zsh')
    const linux = fakeRunner(dump)
    await resolveShellEnv({ env: {}, platform: 'linux', run: linux.run, markers: M })
    expect(linux.calls[0]!.shell).toBe('/bin/bash')
  })

  it('does not spawn anything on Windows', async () => {
    const env: NodeJS.ProcessEnv = {}
    const { run, calls } = fakeRunner(dump)
    expect(await applyShellEnv({ env, platform: 'win32', run, markers: M })).toEqual([])
    expect(calls).toHaveLength(0)
    expect(env).toEqual({})
  })

  it('does not spawn when every name is already set', async () => {
    const env: NodeJS.ProcessEnv = Object.fromEntries(ENGINE_LLM_ENV.map((n) => [n, 'set']))
    const { run, calls } = fakeRunner(dump)
    expect(await applyShellEnv({ env, platform: 'darwin', run, markers: M })).toEqual([])
    expect(calls).toHaveLength(0)
  })

  it('resolves nothing, without throwing, when the shell fails, times out or the runner blows up', async () => {
    const env: NodeJS.ProcessEnv = { SHELL: '/bin/zsh' }
    expect(await applyShellEnv({ env, platform: 'darwin', run: fakeRunner(null).run, markers: M })).toEqual([])
    const rejecting: ShellRunner = () => Promise.reject(new Error('boom'))
    expect(await applyShellEnv({ env, platform: 'darwin', run: rejecting, markers: M })).toEqual([])
    const throwing: ShellRunner = () => {
      throw new Error('boom')
    }
    expect(await applyShellEnv({ env, platform: 'darwin', run: throwing, markers: M })).toEqual([])
    expect(env).toEqual({ SHELL: '/bin/zsh' })
  })
})

describe.skipIf(process.platform === 'win32')('runShell (real child process, fake shells)', () => {
  const withShell = async (body: string, fn: (shell: string) => Promise<void>): Promise<void> => {
    const dir = mkdtempSync(join(tmpdir(), 'polaris-shell-env-'))
    const shell = join(dir, 'fakeshell')
    writeFileSync(shell, `#!/bin/sh\n${body}\n`)
    chmodSync(shell, 0o755)
    try {
      await fn(shell)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  }

  it('end to end: banner on stdout, real env dump between the markers', async () => {
    // $1 is -ilc, $2 the script; the fake shell prints a banner and adds two variables
    const body = [
      'echo "*** banner: ANTHROPIC_API_KEY=banner ***"',
      'export ANTHROPIC_API_KEY="k=1=2"',
      'export UNRELATED_SECRET=zzz',
      'exec /bin/sh -c "$2"',
    ].join('\n')
    await withShell(body, async (shell) => {
      const env: NodeJS.ProcessEnv = { PATH: process.env.PATH, SHELL: shell, LITELLM_API_KEY: 'kept' }
      const applied = await applyShellEnv({ env, platform: process.platform, timeoutMs: 10_000 })
      expect(applied).toEqual(['ANTHROPIC_API_KEY'])
      expect(env.ANTHROPIC_API_KEY).toBe('k=1=2')
      expect(env.LITELLM_API_KEY).toBe('kept')
      expect(env.UNRELATED_SECRET).toBeUndefined()
    })
  }, 20_000)

  it('kills a hanging shell at the timeout and resolves null', async () => {
    await withShell('sleep 30', async (shell) => {
      const started = Date.now()
      expect(await runShell(shell, ['-ilc', 'true'], { timeoutMs: 300, env: { PATH: process.env.PATH } })).toBeNull()
      expect(Date.now() - started).toBeLessThan(5_000)
    })
  }, 20_000)

  it('resolves null on a non-zero exit and on a missing shell', async () => {
    await withShell('echo partial; exit 3', async (shell) => {
      expect(await runShell(shell, [], { timeoutMs: 5_000, env: {} })).toBeNull()
    })
    expect(await runShell('/nonexistent/polaris-no-such-shell', [], { timeoutMs: 5_000, env: {} })).toBeNull()
  }, 20_000)
})
