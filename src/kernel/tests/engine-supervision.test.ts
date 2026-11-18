/* What happens to the engine after it became healthy. Before this, nothing
   watched it: a crash mid-session left the service looking fine while every
   request failed, until the user restarted the app by hand. */
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import { createKernel, legacyEngine, type EngineStateEvent, type LegacyEngineService } from '../src/index.ts'

const dir = mkdtempSync(join(tmpdir(), 'kernel-supervise-'))
afterAll(() => rmSync(dir, { recursive: true, force: true }))

const until = async (cond: () => boolean, ms = 10_000): Promise<void> => {
  const start = Date.now()
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error('condition not met in time')
    await new Promise((r) => setTimeout(r, 25))
  }
}

function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const server = createServer()
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as { port: number }
      server.close(() => resolve(port))
    })
  })
}

/**
 * Fake engine: answers /api/health with its instance id and pid.
 * GET /hang makes it stop listening while the process stays alive (a hung
 * engine); it also prints a line containing two secrets from its environment.
 */
function fakeEngine(port: number): string[] {
  const script = [
    "const http = require('node:http');",
    "console.log('pid=' + process.pid);",
    "console.log('leaky line: ' + process.env.POLARIS_LOCAL_SESSION_SECRET + ' / ' + (process.env.SUPERVISE_TEST_API_KEY || ''));",
    'const srv = http.createServer((req, res) => {',
    "  if (req.url === '/hang') { res.end('ok'); srv.close(); srv.closeAllConnections(); return; }",
    "  res.setHeader('content-type', 'application/json');",
    "  res.end(JSON.stringify({ status: 'ok', version: 'fake', pid: process.pid, instance_id: process.env.POLARIS_INSTANCE_ID || '' }));",
    '});',
    `srv.listen(${port}, '127.0.0.1');`,
    'setInterval(() => {}, 1000);',
  ].join('\n')
  return [process.execPath, '-e', script]
}

async function enginePid(baseUrl: string): Promise<number> {
  const body = (await (await fetch(`${baseUrl}/api/health`)).json()) as { pid: number }
  return body.pid
}

async function start(extra: Record<string, unknown> = {}) {
  const port = await freePort()
  const kernel = createKernel({ name: 'supervise' })
  await kernel.start()
  await kernel.ctx.plugin(legacyEngine, {
    mode: 'command',
    command: fakeEngine(port),
    port,
    healthTimeoutMs: 15_000,
    restartBackoffMs: 50,
    probeIntervalMs: 100,
    probeFailures: 2,
    ...extra,
  })
  const legacy = kernel.ctx.get('legacy') as LegacyEngineService
  const events: EngineStateEvent[] = []
  legacy.onState((event) => events.push(event))
  return { kernel, legacy, events, port }
}

describe('engine supervision after healthy', () => {
  it('restarts a crashed engine once, tells listeners, and keeps the same session', async () => {
    const pidFile = join(dir, 'restart.pid.json')
    const { kernel, legacy, events } = await start({ pidFile })
    try {
      expect(legacy.state()).toBe('running')
      const first = await enginePid(legacy.baseUrl)
      const { instanceId, sessionSecret } = legacy

      process.kill(first, 'SIGKILL')
      await until(() => events.some((event) => event.state === 'running'))

      expect(events.map((event) => event.state)).toEqual(['restarting', 'running'])
      expect(events[0]!.reason).toMatch(/exited unexpectedly/)
      expect(events[0]!.restarts).toBe(1)
      expect(legacy.state()).toBe('running')

      const second = await enginePid(legacy.baseUrl)
      expect(second).not.toBe(first)
      expect(pidAlive(first)).toBe(false)
      // the renderer's session secret and the orphan record stay valid
      expect(legacy.instanceId).toBe(instanceId)
      expect(legacy.sessionSecret).toBe(sessionSecret)
      expect(JSON.parse(readFileSync(pidFile, 'utf8'))).toEqual({ pid: second, instanceId })
      expect(legacy.lastError()).toMatch(/exited unexpectedly/)

      await kernel.stop()
      await until(() => !pidAlive(second))
      expect(existsSync(pidFile)).toBe(false)
    } finally {
      await kernel.stop()
    }
  }, 30_000)

  it('gives up after the restart budget is spent and leaves no process behind', async () => {
    const pidFile = join(dir, 'giveup.pid.json')
    const { kernel, legacy, events } = await start({ pidFile })
    try {
      process.kill(await enginePid(legacy.baseUrl), 'SIGKILL')
      await until(() => events.some((event) => event.state === 'running'))
      const second = await enginePid(legacy.baseUrl)

      process.kill(second, 'SIGKILL')
      await until(() => legacy.state() === 'failed')

      expect(events.map((event) => event.state)).toEqual(['restarting', 'running', 'failed'])
      expect(legacy.lastError()).toMatch(/not restarting again/)
      expect(pidAlive(second)).toBe(false)
      expect(existsSync(pidFile)).toBe(false)
      // nothing is listening any more
      await expect(fetch(`${legacy.baseUrl}/api/health`)).rejects.toThrow()
    } finally {
      await kernel.stop()
    }
  }, 30_000)

  it('does not restart at all when maxRestarts is 0', async () => {
    const { kernel, legacy, events } = await start({ maxRestarts: 0 })
    try {
      process.kill(await enginePid(legacy.baseUrl), 'SIGKILL')
      await until(() => legacy.state() === 'failed')
      expect(events.map((event) => event.state)).toEqual(['failed'])
    } finally {
      await kernel.stop()
    }
  }, 30_000)

  it('detects an engine that is alive but stopped answering, and replaces it', async () => {
    const { kernel, legacy, events } = await start()
    try {
      const first = await enginePid(legacy.baseUrl)
      await fetch(`${legacy.baseUrl}/hang`).catch(() => undefined)
      expect(pidAlive(first)).toBe(true)

      await until(() => events.some((event) => event.state === 'running'), 20_000)
      expect(events.map((event) => event.state)).toEqual(['restarting', 'running'])
      expect(pidAlive(first)).toBe(false)
      expect(await enginePid(legacy.baseUrl)).not.toBe(first)
      expect(legacy.logTail()).toMatch(/stopped answering health checks/)
    } finally {
      await kernel.stop()
    }
  }, 40_000)

  it('stops supervising on dispose: a kill during shutdown is not treated as a crash', async () => {
    const { kernel, legacy, events } = await start()
    const pid = await enginePid(legacy.baseUrl)
    await kernel.stop()
    await until(() => !pidAlive(pid))
    await new Promise((r) => setTimeout(r, 300))
    expect(events).toEqual([])
  }, 30_000)
})

describe('engine start edge cases', () => {
  it('rejects instead of crashing the host when the executable does not exist', async () => {
    const kernel = createKernel({ name: 'enoent' })
    await kernel.start()
    let message = ''
    try {
      await kernel.ctx.plugin(legacyEngine, {
        mode: 'command',
        command: [join(dir, 'no-such-python'), '-c', 'pass'],
        port: await freePort(),
        healthTimeoutMs: 10_000,
      })
    } catch (err) {
      message = String(err)
    }
    expect(message).toMatch(/could not be started/)
    expect(kernel.ctx.get('legacy')).toBeUndefined()
    await kernel.stop()
  }, 20_000)

  it('abandons the health wait as soon as the kernel is stopped (quit during startup)', async () => {
    const kernel = createKernel({ name: 'cancel' })
    await kernel.start()
    const pending = Promise.resolve(
      kernel.ctx.plugin(legacyEngine, {
        mode: 'command',
        // alive, never listens: without cancellation this waits the full timeout
        command: [process.execPath, '-e', 'setInterval(() => {}, 1000)'],
        port: await freePort(),
        healthTimeoutMs: 60_000,
      }),
    ).then(
      () => 'resolved',
      (err: unknown) => String(err),
    )
    await new Promise((r) => setTimeout(r, 400))
    const t0 = Date.now()
    await kernel.stop()
    const outcome = await pending
    expect(Date.now() - t0).toBeLessThan(8_000)
    expect(outcome).not.toBe('resolved')
  }, 20_000)
})

describe('engine log file', () => {
  it('writes engine output to the log file with secrets scrubbed', async () => {
    const logFile = join(dir, 'logs', 'engine.log')
    process.env.SUPERVISE_TEST_API_KEY = 'user-api-key-that-must-not-leak'
    try {
      const { kernel, legacy } = await start({ logFile })
      try {
        await until(() => existsSync(logFile) && readFileSync(logFile, 'utf8').includes('leaky line'))
        const text = readFileSync(logFile, 'utf8')
        expect(text).toMatch(/pid=\d+/)
        expect(text).not.toContain('user-api-key-that-must-not-leak')
        expect(text).not.toContain(legacy.sessionSecret)
        expect(text).toContain('[redacted:SUPERVISE_TEST_API_KEY]')
        expect(text).toContain('[redacted:POLARIS_LOCAL_SESSION_SECRET]')

        // supervision notes land in the same file
        process.kill(await enginePid(legacy.baseUrl), 'SIGKILL')
        await until(() => readFileSync(logFile, 'utf8').includes('[polaris] engine running'))
        expect(readFileSync(logFile, 'utf8')).toMatch(/\[polaris\] engine restarting: engine process exited unexpectedly/)
      } finally {
        await kernel.stop()
      }
    } finally {
      delete process.env.SUPERVISE_TEST_API_KEY
    }
  }, 30_000)
})
