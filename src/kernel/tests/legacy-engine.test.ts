/* legacy-engine 插件的单元测试：只测 command 模式，故意不碰 docker——
   docker 形态由 desktop 冒烟（POLARIS_SMOKE_ENGINE=1）覆盖，这里保证
   在任何 CI 机器上都能跑。假引擎用 node -e 起一个最小 HTTP 服务器。 */
import { spawn } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { createServer, type Server } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  ENGINE_CONTAINER,
  ENGINE_SECRET_ENV,
  buildEngineArgv,
  createKernel,
  enginePortProblem,
  generateFernetKey,
  legacyEngine,
  loadOrCreateEngineSecrets,
  type LegacyEngineService,
} from '../src/index.ts'

const until = async (cond: () => boolean, ms = 5_000): Promise<void> => {
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

/** 假引擎脚本：/api/health 返回 {"status":"ok"}，并把 pid 打到 stdout，
    让测试能经 logTail() 拿到真实进程号做「dispose 后确实死了」的断言。 */
function fakeEngineScript(port: number): string {
  return [
    "const http = require('node:http');",
    "console.log('pid=' + process.pid);",
    'const srv = http.createServer((req, res) => {',
    "  res.setHeader('content-type', 'application/json');",
    // 顺带回显 fake 回退开关：断言 command 模式**没有**代设 POLARIS_LLM_FAKE_FALLBACK（#717）
    "  res.end(JSON.stringify({ status: 'ok', version: 'fake', instance_id: process.env.POLARIS_INSTANCE_ID || '',",
    "    session: process.env.POLARIS_LOCAL_SESSION_SECRET || '', enc: process.env.POLARIS_ENCRYPTION_KEY || '',",
    "    fake: process.env.POLARIS_LLM_FAKE_FALLBACK || '' }));",
    '});',
    `srv.listen(${port}, '127.0.0.1');`,
  ].join('\n')
}

describe('legacy-engine plugin (command mode)', () => {
  it('provides the legacy service once healthy and kills the process on dispose', async () => {
    // 开发者 shell 里若设过该变量会经 ...process.env 泄漏进假引擎，先清掉，
    // 保证「插件不代设」的断言测的是插件而不是运行环境
    delete process.env.POLARIS_LLM_FAKE_FALLBACK
    const port = 21000 + Math.floor(Math.random() * 9000)
    const kernel = createKernel({ name: 'engine-test' })
    await kernel.start()

    await kernel.ctx.plugin(legacyEngine, {
      mode: 'command',
      command: [process.execPath, '-e', fakeEngineScript(port)],
      port,
      healthTimeoutMs: 15_000,
    })

    const legacy = kernel.ctx.get('legacy') as LegacyEngineService | undefined
    expect(legacy).toBeTruthy()
    expect(legacy!.baseUrl).toBe(`http://127.0.0.1:${port}`)

    const res = await fetch(`${legacy!.baseUrl}/api/health`)
    expect(res.ok).toBe(true)
    const body = (await res.json()) as { status: string; fake: string; instance_id: string; session: string }
    expect(body.status).toBe('ok')
    // 实例标识与会话口令经 env 交给引擎，服务上暴露同一份（#850）
    expect(body.instance_id).toBe(legacy!.instanceId)
    expect(legacy!.instanceId).toMatch(/^[0-9a-f]{32}$/)
    expect(body.session).toBe(legacy!.sessionSecret)
    expect(legacy!.sessionSecret.length).toBeGreaterThanOrEqual(40)
    // fake LLM 回退是严格显式 opt-in：插件绝不代设（#717）。本测试进程没设
    // 该变量，引擎子进程里也必须不存在。
    expect(body.fake).toBe('')

    // stdout 是异步管道，pid 行可能晚于 health 就绪一拍
    await until(() => /pid=\d+/.test(legacy!.logTail()))
    const pid = Number(/pid=(\d+)/.exec(legacy!.logTail())![1])
    expect(pidAlive(pid)).toBe(true)

    await kernel.stop()
    await until(() => !pidAlive(pid))
  }, 30_000)

  it('fails the fiber (and reclaims the child) when health never comes up', async () => {
    const port = 21000 + Math.floor(Math.random() * 9000)
    const kernel = createKernel({ name: 'engine-timeout' })
    await kernel.start()

    // 进程活着但从不监听端口：健康轮询必须在超时后抛错、fiber 进 FAILED
    let message = ''
    try {
      await kernel.ctx.plugin(legacyEngine, {
        mode: 'command',
        command: [process.execPath, '-e', "console.log('pid=' + process.pid); setInterval(() => {}, 1000);"],
        port,
        healthTimeoutMs: 1_500,
      })
    } catch (err) {
      message = String(err)
    }
    expect(message).toMatch(/did not become healthy/)
    // 错误信息必须带日志尾巴（排错的关键承诺），顺便从里面拿到真实 pid
    const pid = Number(/pid=(\d+)/.exec(message)![1])
    expect(kernel.ctx.get('legacy')).toBeUndefined()
    // 失败路径也不能留孤儿：disposer 已随 fiber 失败执行
    await until(() => !pidAlive(pid))
    await kernel.stop()
  }, 15_000)
})

/* docker 模式不真跑容器（那是 desktop 冒烟/E2E 的事），但 argv 拼装要有
   回归护栏：容器名可覆盖是 E2E 与并行套件互不撞名的前提。 */
describe('legacy-engine buildEngineArgv (docker mode)', () => {
  const base = { mode: 'docker' as const, image: 'img:tag', backendDir: '/abs/backend' }

  it('uses the default container name and wires image/mount/port', () => {
    const argv = buildEngineArgv(base, 18080)
    expect(argv.slice(0, 5)).toEqual(['docker', 'run', '--rm', '--name', ENGINE_CONTAINER])
    expect(argv).toContain('img:tag')
    expect(argv).toContain('/abs/backend:/srv/backend')
    expect(argv).toContain('127.0.0.1:18080:8000')
  })

  it('never hardcodes the fake LLM fallback; only a valueless pass-through (#717)', () => {
    const argv = buildEngineArgv(base, 18080)
    // 无值 -e：宿主显式设了才透传进容器，插件自身永不把回退设成开
    expect(argv).toContain('POLARIS_LLM_FAKE_FALLBACK')
    expect(argv.join(' ')).not.toContain('POLARIS_LLM_FAKE_FALLBACK=')
  })

  it('passes the database URL through without a value so the host can redirect it (#687)', () => {
    const argv = buildEngineArgv(base, 18080)
    // 不指定时后端仍用默认的 ./polaris_dev.db（挂载目录里那一个）；宿主
    // （E2E、引导流程）显式设了才改道——插件不替任何人决定库放在哪
    expect(argv).toContain('POLARIS_DATABASE_URL')
    expect(argv.join(' ')).not.toContain('POLARIS_DATABASE_URL=')
  })

  it('honors a custom containerName so parallel instances do not collide', () => {
    const argv = buildEngineArgv({ ...base, containerName: 'polaris-engine-e2e-x' }, 19000)
    expect(argv.slice(3, 5)).toEqual(['--name', 'polaris-engine-e2e-x'])
    expect(argv).toContain('127.0.0.1:19000:8000')
    expect(argv).not.toContain(ENGINE_CONTAINER)
  })
})

/* ---- #850：每份安装的密钥、端口归属 ---- */

const randomPort = (): number => 21000 + Math.floor(Math.random() * 9000)

function listen(port: number, handler: Parameters<typeof createServer>[1]): Promise<Server> {
  return new Promise((resolve) => {
    const srv = createServer(handler)
    srv.listen(port, '127.0.0.1', () => resolve(srv))
  })
}

async function waitHealthy(port: number): Promise<void> {
  for (let i = 0; i < 100; i++) {
    const ok = await fetch(`http://127.0.0.1:${port}/api/health`).then(
      (r) => r.ok,
      () => false,
    )
    if (ok) return
    await new Promise((r) => setTimeout(r, 50))
  }
  throw new Error('fake engine never came up')
}

describe('engine secrets file (#850)', () => {
  it('generates a private, stable per-install secret pair with a valid Fernet key', () => {
    const dir = mkdtempSync(join(tmpdir(), 'polaris-secrets-'))
    try {
      const path = join(dir, 'nested', 'engine-secrets.json')
      const first = loadOrCreateEngineSecrets(path)
      expect(first.encryptionKey).toMatch(/^[A-Za-z0-9_-]{43}=$/)
      expect(Buffer.from(first.encryptionKey, 'base64url')).toHaveLength(32)
      expect(first.secretKey.length).toBeGreaterThanOrEqual(32)
      if (process.platform !== 'win32') expect(statSync(path).mode & 0o777).toBe(0o600)
      // 第二次读出同一份：换了密钥库里的密文就全解不开了
      expect(loadOrCreateEngineSecrets(path)).toEqual(first)
      expect(generateFernetKey()).not.toBe(generateFernetKey())

      // 损坏的文件另存一份再重新生成，而不是让引擎永远起不来
      writeFileSync(path, '{not json')
      const regenerated = loadOrCreateEngineSecrets(path)
      expect(regenerated.encryptionKey).not.toBe(first.encryptionKey)
      expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual(regenerated)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('hands the per-install keys to the engine through the environment', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'polaris-secrets-'))
    const port = randomPort()
    const kernel = createKernel({ name: 'engine-secrets' })
    await kernel.start()
    try {
      const secretsFile = join(dir, 'engine-secrets.json')
      await kernel.ctx.plugin(legacyEngine, {
        mode: 'command',
        command: [process.execPath, '-e', fakeEngineScript(port)],
        port,
        healthTimeoutMs: 15_000,
        secretsFile,
      })
      const body = (await (await fetch(`http://127.0.0.1:${port}/api/health`)).json()) as { enc: string }
      expect(body.enc).toBe(loadOrCreateEngineSecrets(secretsFile).encryptionKey)
    } finally {
      await kernel.stop()
      rmSync(dir, { recursive: true, force: true })
    }
  }, 30_000)

  it('passes the secrets to docker by name only, never by value', () => {
    const argv = buildEngineArgv({ mode: 'docker', image: 'img', backendDir: '/abs' }, 18080)
    for (const name of ENGINE_SECRET_ENV) expect(argv).toContain(name)
    expect(argv.join(' ')).not.toMatch(/POLARIS_(INSTANCE_ID|LOCAL_SESSION_SECRET|SECRET_KEY|ENCRYPTION_KEY)=/)
  })
})

describe('engine port ownership (#850)', () => {
  async function startExpectingFailure(port: number, pidFile?: string): Promise<unknown> {
    const kernel = createKernel({ name: 'engine-port' })
    await kernel.start()
    let error: unknown = null
    try {
      await kernel.ctx.plugin(legacyEngine, {
        mode: 'command',
        command: [process.execPath, '-e', fakeEngineScript(port)],
        port,
        healthTimeoutMs: 5_000,
        pidFile,
      })
    } catch (err) {
      error = err
    }
    expect(kernel.ctx.get('legacy')).toBeUndefined()
    await kernel.stop()
    return error
  }

  it('reports an older Polaris engine holding the port instead of trusting it', async () => {
    const port = randomPort()
    // 没有 instance_id 的 Polaris 式应答：修复前的旧引擎
    const srv = await listen(port, (_req, res) => {
      res.setHeader('content-type', 'application/json')
      res.end(JSON.stringify({ status: 'ok', version: '0.5.0' }))
    })
    try {
      const err = await startExpectingFailure(port)
      expect(enginePortProblem(err)).toBe('stale-engine')
    } finally {
      srv.close()
    }
  }, 20_000)

  it('reports a foreign program holding the port', async () => {
    const port = randomPort()
    const srv = await listen(port, (_req, res) => {
      res.statusCode = 404
      res.end('<html>not polaris</html>')
    })
    try {
      const err = await startExpectingFailure(port)
      expect(enginePortProblem(err)).toBe('port-in-use')
      expect(String(err)).toMatch(/in use by another program/)
    } finally {
      srv.close()
    }
  }, 20_000)

  it('stops its own orphaned engine (pid file + matching instance id) and starts a fresh one', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'polaris-pid-'))
    const port = randomPort()
    const pidFile = join(dir, 'engine.pid.json')
    const orphan = spawn(process.execPath, ['-e', fakeEngineScript(port)], {
      env: { ...process.env, POLARIS_INSTANCE_ID: 'orphan-instance' },
      stdio: 'ignore',
    })
    const kernel = createKernel({ name: 'engine-orphan' })
    try {
      await waitHealthy(port)
      writeFileSync(pidFile, JSON.stringify({ pid: orphan.pid, instanceId: 'orphan-instance' }))
      await kernel.start()
      await kernel.ctx.plugin(legacyEngine, {
        mode: 'command',
        command: [process.execPath, '-e', fakeEngineScript(port)],
        port,
        healthTimeoutMs: 15_000,
        pidFile,
      })
      const legacy = kernel.ctx.get('legacy') as LegacyEngineService
      await until(() => orphan.exitCode !== null || orphan.signalCode !== null)
      const body = (await (await fetch(`${legacy.baseUrl}/api/health`)).json()) as { instance_id: string }
      expect(body.instance_id).toBe(legacy.instanceId)
      // pid 文件改记新的这一个
      expect(JSON.parse(readFileSync(pidFile, 'utf8')).instanceId).toBe(legacy.instanceId)
      await kernel.stop()
      // 正常退出后删掉自己的记录
      expect(() => statSync(pidFile)).toThrow()
    } finally {
      orphan.kill('SIGKILL')
      await kernel.stop()
      rmSync(dir, { recursive: true, force: true })
    }
  }, 30_000)

  it('never kills a process whose instance id does not match the pid file', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'polaris-pid-'))
    const port = randomPort()
    const pidFile = join(dir, 'engine.pid.json')
    const other = spawn(process.execPath, ['-e', fakeEngineScript(port)], {
      env: { ...process.env, POLARIS_INSTANCE_ID: 'someone-else' },
      stdio: 'ignore',
    })
    try {
      await waitHealthy(port)
      writeFileSync(pidFile, JSON.stringify({ pid: other.pid, instanceId: 'recorded-instance' }))
      const err = await startExpectingFailure(port, pidFile)
      expect(enginePortProblem(err)).toBe('stale-engine')
      expect(other.exitCode).toBeNull()
      expect(other.signalCode).toBeNull()
    } finally {
      other.kill('SIGKILL')
      rmSync(dir, { recursive: true, force: true })
    }
  }, 30_000)
})
