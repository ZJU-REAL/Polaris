/* ============================================================
   legacy-engine 插件：把现有 Python 后端作为受监督子进程拉起。

   P0 Spike-1 原型（spikes/p0/1-legacy-chain）的转正版，但大幅简化：
   后端是单进程（SQLite + 进程内任务队列，见 #583），所以只拉一个 api
   进程——不需要 redis 容器、不需要 worker 容器、不需要共享卷。

   两种模式：
   - docker：桌面端主用形态。容器名固定，disposer 里 `docker rm -f`
     兜底：attached 的 docker 客户端死了容器未必跟着死，按名字删才是
     可靠回收（Spike-1 的结论）。
   - command：直接 spawn 给定 argv。单元测试用 node -e 起假引擎即可
     全程不依赖 docker，也是未来裸进程发行形态的座位。

   健康轮询通过后以 ctx.provide('legacy') 挂出服务；失败则抛错让
   fiber 进 FAILED（cordis 失败路径会照跑 disposer，子进程不会孤儿）。

   安全（#850）：每次启动生成实例标识与本地会话口令、按 secretsFile 读出
   每份安装的密钥，全部经环境变量交给引擎；健康检查只认带着本次实例标识
   的应答——端口被残留旧引擎或别的程序占着时报 EnginePortError，而不是把
   别人的服务当成自己的。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import { execFileSync, spawn, type ChildProcess } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { chmodSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { createConnection } from 'node:net'
import { dirname } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import Schema from '@deepseek-ai/schemastery'
import type { Context } from '@deepseek-ai/cordis'

/** docker 模式的默认容器名：可预测的名字才能在 disposer 与冒烟里精确回收。 */
export const ENGINE_CONTAINER = 'polaris-desktop-engine'

/** 环形日志缓冲的行数上限。取「一屏多一点」：够定位启动失败，不吃内存。 */
const LOG_LIMIT = 200

export interface LegacyEngineConfig {
  mode: 'command' | 'docker'
  /** command 模式：完整 argv（[0] 是可执行文件）。 */
  command?: string[]
  /** docker 模式：镜像名（可带 tag）。 */
  image?: string
  /** docker 模式：挂到容器 /srv/backend 的后端源码目录（绝对路径）。 */
  backendDir?: string
  /** docker 模式：容器名，默认 ENGINE_CONTAINER。同机并行多个实例（如壳级
      E2E 与手动开发实例同时在跑）时必须各用各的名字，否则 docker run 直接
      撞名失败、disposer 还会误删别人的容器。 */
  containerName?: string
  /** 宿主侧监听端口（只绑 127.0.0.1）。 */
  port?: number
  /** 健康轮询超时。首启要跑全部 alembic 迁移，默认给足两分钟。 */
  healthTimeoutMs?: number
  /** 每份安装独立的引擎密钥文件（#850）：首启生成（权限 0600），之后每次启动读出来经
      POLARIS_SECRET_KEY / POLARIS_ENCRYPTION_KEY 交给引擎。配置里只放路径，密钥本身
      不进配置——plugins.list 会把条目配置原样交给渲染进程。不设则不传（单测/假引擎）。 */
  secretsFile?: string
  /** 记录「本插件拉起的引擎」的 pid 与实例标识（#850）。上次异常退出留下的孤儿引擎
      占着端口时，凭它确认是自己的旧进程后再收掉；不设则从不杀任何进程。 */
  pidFile?: string
}

export const LegacyEngineConfig = Schema.object({
  mode: Schema.union(['command', 'docker'] as const).required(),
  command: Schema.array(String),
  image: Schema.string(),
  backendDir: Schema.string(),
  containerName: Schema.string(),
  port: Schema.number().default(18080),
  healthTimeoutMs: Schema.number().default(120_000),
  secretsFile: Schema.string(),
  pidFile: Schema.string(),
})

export interface LegacyEngineService {
  /** 本地后端根地址，如 http://127.0.0.1:18080。 */
  baseUrl: string
  /** 最近约 200 行 stdout/stderr（alembic / uvicorn 的启动横幅都在这），排错用。 */
  logTail(): string
  /** 本次启动的实例标识（POLARIS_INSTANCE_ID），/api/health 原样返回。 */
  instanceId: string
  /** 本次启动的本地会话口令（POLARIS_LOCAL_SESSION_SECRET）：只交给宿主自己的界面，
      引擎的 /auth/local-session 只认带着它的请求（#850）。 */
  sessionSecret: string
}

/** 插件交给引擎的环境变量。docker 模式按名字无值透传（值不出现在 argv / ps 里）。 */
export const ENGINE_SECRET_ENV = [
  'POLARIS_INSTANCE_ID',
  'POLARIS_LOCAL_SESSION_SECRET',
  'POLARIS_SECRET_KEY',
  'POLARIS_ENCRYPTION_KEY',
] as const

/* ---- 每份安装独立的密钥（#850） ---- */

export interface EngineSecrets {
  /** JWT / 下载链接签名用。 */
  secretKey: string
  /** Fernet key：32 字节的 urlsafe base64（带 = 填充，Python 端严格校验填充）。 */
  encryptionKey: string
}

const FERNET_KEY_RE = /^[A-Za-z0-9_-]{43}=$/

/** 生成合法的 Fernet key（等价于 Python 的 Fernet.generate_key()）。 */
export function generateFernetKey(): string {
  return randomBytes(32).toString('base64').replace(/\+/g, '-').replace(/\//g, '_')
}

function isEngineSecrets(value: unknown): value is EngineSecrets {
  const v = value as Partial<EngineSecrets> | null
  return (
    typeof v?.secretKey === 'string' &&
    v.secretKey.length >= 32 &&
    typeof v.encryptionKey === 'string' &&
    FERNET_KEY_RE.test(v.encryptionKey)
  )
}

/** 原子写一个只有本人可读的文件（先写临时文件再改名，中途断电不会留下半个文件）。 */
function writePrivateFile(path: string, text: string): void {
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.${process.pid}.tmp`
  writeFileSync(tmp, text, { mode: 0o600 })
  renameSync(tmp, path)
  try {
    chmodSync(path, 0o600)
  } catch {
    /* Windows 上权限位无意义（userData 本就在本人的配置目录里） */
  }
}

/**
 * 读出（首启则生成）引擎密钥。文件读不了（权限等）直接抛错——绝不能拿新密钥覆盖
 * 旧文件，否则库里已加密的凭据全部解不开。内容损坏才另存一份 .corrupt-* 再重新生成。
 */
export function loadOrCreateEngineSecrets(path: string): EngineSecrets {
  let raw: string | null = null
  try {
    raw = readFileSync(path, 'utf8')
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err
  }
  if (raw !== null) {
    let parsed: unknown = null
    try {
      parsed = JSON.parse(raw)
    } catch {
      /* 落到下面按损坏处理 */
    }
    if (isEngineSecrets(parsed)) {
      try {
        chmodSync(path, 0o600)
      } catch {
        /* 同上 */
      }
      return { secretKey: parsed.secretKey, encryptionKey: parsed.encryptionKey }
    }
    renameSync(path, `${path}.corrupt-${Date.now()}`)
  }
  const secrets: EngineSecrets = {
    secretKey: randomBytes(48).toString('base64url'),
    encryptionKey: generateFernetKey(),
  }
  writePrivateFile(path, `${JSON.stringify(secrets, null, 2)}\n`)
  return secrets
}

/* ---- 端口归属（#850）：确认端口上应答的是自己 ---- */

/** 端口被别人占着时的失败原因。kernel 宿主据此给出具体提示。 */
export type EnginePortProblem = 'port-in-use' | 'stale-engine'

const PORT_PROBLEM_MARKER = 'ENGINE_PORT_PROBLEM:'

export class EnginePortError extends Error {
  readonly code = 'ENGINE_PORT_PROBLEM'
  constructor(
    readonly problem: EnginePortProblem,
    readonly port: number,
  ) {
    super(
      `${PORT_PROBLEM_MARKER}${problem} — ` +
        (problem === 'stale-engine'
          ? `an older Polaris engine is still running on 127.0.0.1:${port}`
          : `port ${port} on 127.0.0.1 is in use by another program`),
    )
    this.name = 'EnginePortError'
  }
}

/** 从（可能被 cordis 包过一层的）错误里认出端口问题。 */
export function enginePortProblem(err: unknown): EnginePortProblem | null {
  const seen = new Set<unknown>()
  const visit = (e: unknown): EnginePortProblem | null => {
    if (e == null || seen.has(e)) return null
    seen.add(e)
    if (e instanceof EnginePortError) return e.problem
    const text = e instanceof Error ? e.message : String(e)
    const m = /ENGINE_PORT_PROBLEM:(port-in-use|stale-engine)/.exec(text)
    if (m) return m[1] as EnginePortProblem
    if (e instanceof AggregateError) {
      for (const inner of e.errors) {
        const found = visit(inner)
        if (found) return found
      }
    }
    return e instanceof Error ? visit(e.cause) : null
  }
  return visit(err)
}

/** 127.0.0.1:port 上有没有人在监听。 */
export function portInUse(port: number, timeoutMs = 1_000): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = createConnection({ host: '127.0.0.1', port })
    const done = (busy: boolean): void => {
      socket.destroy()
      resolve(busy)
    }
    socket.setTimeout(timeoutMs, () => done(false))
    socket.once('connect', () => done(true))
    socket.once('error', () => done(false))
  })
}

interface HealthAnswer {
  /** 看起来是 Polaris 引擎（{status:'ok', version}）。 */
  polaris: boolean
  instanceId: string | null
}

async function probeHealth(baseUrl: string): Promise<HealthAnswer | null> {
  try {
    const res = await fetch(`${baseUrl}/api/health`, { signal: AbortSignal.timeout(3_000) })
    const body = (await res.json().catch(() => null)) as Record<string, unknown> | null
    return {
      polaris: res.ok && body?.status === 'ok' && typeof body.version === 'string',
      instanceId: typeof body?.instance_id === 'string' && body.instance_id ? body.instance_id : null,
    }
  } catch {
    return null
  }
}

interface PidRecord {
  pid: number
  instanceId: string
  containerName?: string
}

function readPidFile(path: string): PidRecord | null {
  try {
    const v = JSON.parse(readFileSync(path, 'utf8')) as Partial<PidRecord>
    if (typeof v.pid === 'number' && typeof v.instanceId === 'string' && v.instanceId) {
      return { pid: v.pid, instanceId: v.instanceId, containerName: v.containerName }
    }
  } catch {
    /* 没有或损坏：当作没有记录 */
  }
  return null
}

function removePidFile(path: string, instanceId?: string): void {
  // 只删自己写的那份：之后的另一次启动可能已经写了新的
  if (instanceId !== undefined && readPidFile(path)?.instanceId !== instanceId) return
  rmSync(path, { force: true })
}

function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

async function waitPortFree(port: number, ms: number): Promise<boolean> {
  const deadline = Date.now() + ms
  while (Date.now() < deadline) {
    if (!(await portInUse(port))) return true
    await delay(250)
  }
  return !(await portInUse(port))
}

/**
 * 拉起前确认端口空着。占着的若是自己上次留下的孤儿（pid 文件里的实例标识与端口上
 * 应答的一致——顺带防住 pid 被系统复用给别的进程），收掉它；否则抛 EnginePortError，
 * 由宿主告诉用户是谁占了端口。只认实例标识，绝不按端口去杀不认识的进程。
 */
async function reclaimPort(port: number, baseUrl: string, pidFile: string | undefined): Promise<void> {
  if (!(await portInUse(port))) return
  const answer = await probeHealth(baseUrl)
  const record = pidFile ? readPidFile(pidFile) : null
  if (pidFile && record && answer?.instanceId && answer.instanceId === record.instanceId) {
    if (record.containerName) {
      try {
        execFileSync('docker', ['rm', '-f', record.containerName], { stdio: 'ignore' })
      } catch {
        /* 容器已不在 */
      }
    }
    if (pidAlive(record.pid)) {
      try {
        process.kill(record.pid, 'SIGTERM')
      } catch {
        /* 已退出 */
      }
    }
    let freed = await waitPortFree(port, 8_000)
    if (!freed && pidAlive(record.pid)) {
      try {
        process.kill(record.pid, 'SIGKILL')
      } catch {
        /* 已退出 */
      }
      freed = await waitPortFree(port, 4_000)
    }
    if (freed) {
      removePidFile(pidFile)
      return
    }
  }
  throw new EnginePortError(answer?.polaris ? 'stale-engine' : 'port-in-use', port)
}

/** 等子进程退出；超时返回 false（调用方决定是否升级成 SIGKILL）。 */
function exited(child: ChildProcess, ms: number): Promise<boolean> {
  return new Promise((resolve) => {
    if (child.exitCode !== null || child.signalCode !== null) return resolve(true)
    const timer = setTimeout(() => {
      child.off('exit', onExit)
      resolve(false)
    }, ms)
    const onExit = (): void => {
      clearTimeout(timer)
      resolve(true)
    }
    child.once('exit', onExit)
  })
}

/** 导出仅为可测性：docker 模式在单测里不真跑容器，但 argv 的拼装（容器名/
    端口/挂载）必须有回归护栏——E2E 靠自定义容器名与并行套件隔离。 */
export function buildEngineArgv(config: LegacyEngineConfig, port: number): string[] {
  if (config.mode === 'command') {
    if (!config.command?.length) {
      throw new Error('legacy-engine: mode=command 需要非空的 command argv')
    }
    return config.command
  }
  if (!config.image || !config.backendDir) {
    throw new Error('legacy-engine: mode=docker 需要 image 与 backendDir')
  }
  return [
    'docker', 'run', '--rm', '--name', config.containerName ?? ENGINE_CONTAINER,
    '-v', `${config.backendDir}:/srv/backend`,
    '-w', '/srv/backend',
    // 只绑回环地址：本地引擎是单机私有服务，绝不能暴露到局域网
    '-p', `127.0.0.1:${port}:8000`,
    // fake LLM 回退绝不由插件代设（#717，设计报告 §18 信任设计：AI 输出必须
    // 真实可溯源，编不出来就明说）。这里只做无值透传：宿主进程显式设了
    // POLARIS_LLM_FAKE_FALLBACK（冒烟/E2E 的显式 opt-in）docker 才带进容器，
    // 没设则容器内同样不存在——与 command 模式继承宿主 env 的行为对齐。
    '-e', 'POLARIS_LLM_FAKE_FALLBACK',
    // 数据库地址同样只做无值透传（#687）。docker 模式不设它时，后端用默认的
    // ./polaris_dev.db——也就是挂载进来的后端源码目录里那一个，跨次运行留存。
    // command 模式早就把库钉在 userData 下（engine-bootstrap 的启动器，#718），
    // 这里补上同一个口子：宿主显式指定就用宿主的，不指定则维持原状不变。
    '-e', 'POLARIS_DATABASE_URL',
    // 实例标识、会话口令与每份安装的密钥（#850）：同样无值透传，值只在 docker 客户端
    // 的环境里，不进 argv（ps 看得到 argv）
    ...ENGINE_SECRET_ENV.flatMap((name) => ['-e', name]),
    config.image,
    'sh', '-lc',
    // 先迁移后起服务：没有独立 worker 抢跑迁移的问题，串行即可
    'python -m alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000',
  ]
}

export const legacyEngine = {
  name: 'legacy-engine',

  Config: LegacyEngineConfig,

  async apply(ctx: Context, config: LegacyEngineConfig): Promise<void> {
    const port = config.port ?? 18080
    const healthTimeoutMs = config.healthTimeoutMs ?? 120_000
    const baseUrl = `http://127.0.0.1:${port}`

    // 每次启动都换新的实例标识与会话口令（#850）；持久密钥按配置的文件读出/生成
    const instanceId = randomBytes(16).toString('hex')
    const sessionSecret = randomBytes(32).toString('base64url')
    const engineEnv: Record<string, string> = {
      POLARIS_INSTANCE_ID: instanceId,
      POLARIS_LOCAL_SESSION_SECRET: sessionSecret,
    }
    if (config.secretsFile) {
      const secrets = loadOrCreateEngineSecrets(config.secretsFile)
      engineEnv.POLARIS_SECRET_KEY = secrets.secretKey
      engineEnv.POLARIS_ENCRYPTION_KEY = secrets.encryptionKey
    }

    await reclaimPort(port, baseUrl, config.pidFile)

    const lines: string[] = []
    const capture = (chunk: Buffer): void => {
      for (const line of chunk.toString().split('\n')) {
        if (line) lines.push(line)
      }
      if (lines.length > LOG_LIMIT) lines.splice(0, lines.length - LOG_LIMIT)
    }

    let child!: ChildProcess
    // spawn 放进 effect：fiber 被 dispose（kernel.stop / 插件卸载 / 启动失败）
    // 时 disposer 必然执行，子进程不会变孤儿。
    ctx.effect(() => {
      const argv = buildEngineArgv(config, port)
      child = spawn(argv[0]!, argv.slice(1), {
        stdio: ['ignore', 'pipe', 'pipe'],
        // fake LLM 回退是严格显式 opt-in（#717）：只随宿主 env 自然继承，插件不代设。
        env: { ...process.env, ...engineEnv },
      })
      child.stdout!.on('data', capture)
      child.stderr!.on('data', capture)
      if (config.pidFile && child.pid !== undefined) {
        const record: PidRecord = { pid: child.pid, instanceId }
        if (config.mode === 'docker') record.containerName = config.containerName ?? ENGINE_CONTAINER
        try {
          writePrivateFile(config.pidFile, `${JSON.stringify(record)}\n`)
        } catch {
          /* 记不下来只是少了下次收孤儿的依据，不影响这次启动 */
        }
      }
      return async () => {
        if (config.pidFile) removePidFile(config.pidFile, instanceId)
        if (config.mode === 'docker') {
          // 按容器名强删：attached 客户端先死时容器可能残留，名字才是真锚点
          try {
            execFileSync('docker', ['rm', '-f', config.containerName ?? ENGINE_CONTAINER], { stdio: 'ignore' })
          } catch {
            /* 容器已不在 */
          }
        }
        if (child.exitCode === null && child.signalCode === null) {
          child.kill('SIGTERM')
          if (!(await exited(child, 5_000))) {
            child.kill('SIGKILL')
            await exited(child, 5_000)
          }
        }
      }
    }, 'legacy-engine process')

    const deadline = Date.now() + healthTimeoutMs
    for (;;) {
      // 进程先死了就别傻等到超时：把日志尾巴直接带进错误里，省一轮排错
      if (child.exitCode !== null || child.signalCode !== null) {
        // 起不来的常见原因是端口刚被别人抢走（绑定失败）：那就如实报端口问题
        if (await portInUse(port)) {
          const other = await probeHealth(baseUrl)
          throw new EnginePortError(other?.polaris ? 'stale-engine' : 'port-in-use', port)
        }
        throw new Error(
          `legacy engine exited before becoming healthy (code=${child.exitCode}, signal=${child.signalCode})\n${lines.join('\n')}`,
        )
      }
      // 只认自己：应答里必须带着这次启动的实例标识（#850）。别的进程（残留的
      // 旧引擎、恰好占了这个端口的程序）回 200 也不算健康。
      const answer = await probeHealth(baseUrl)
      if (answer?.instanceId === instanceId) break
      if (Date.now() > deadline) {
        throw new Error(
          `legacy engine did not become healthy within ${healthTimeoutMs}ms\n${lines.join('\n')}`,
        )
      }
      await delay(500)
    }

    const service: LegacyEngineService = {
      baseUrl,
      logTail: () => lines.join('\n'),
      instanceId,
      sessionSecret,
    }
    ctx.provide('legacy', service)
  },
}
