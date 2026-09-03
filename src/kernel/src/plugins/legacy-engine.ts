/* ============================================================
   legacy-engine 插件：把现有 Python 后端作为受监督子进程拉起。

   P0 Spike-1 原型（spikes/p0/1-legacy-chain）的转正版，但大幅简化：
   desktop 档位的后端是单进程（POLARIS_PROFILE=desktop = SQLite +
   进程内任务队列 + 进程内 fakeredis，见 #583），所以只拉一个 api
   进程——不需要 redis 容器、不需要 worker 容器、不需要共享卷。

   两种模式：
   - docker：桌面端主用形态。容器名固定，disposer 里 `docker rm -f`
     兜底：attached 的 docker 客户端死了容器未必跟着死，按名字删才是
     可靠回收（Spike-1 的结论）。
   - command：直接 spawn 给定 argv。单元测试用 node -e 起假引擎即可
     全程不依赖 docker，也是未来裸进程发行形态的座位。

   健康轮询通过后以 ctx.provide('legacy') 挂出服务；失败则抛错让
   fiber 进 FAILED（cordis 失败路径会照跑 disposer，子进程不会孤儿）。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import { execFileSync, spawn, type ChildProcess } from 'node:child_process'
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
}

export const LegacyEngineConfig = Schema.object({
  mode: Schema.union(['command', 'docker'] as const).required(),
  command: Schema.array(String),
  image: Schema.string(),
  backendDir: Schema.string(),
  containerName: Schema.string(),
  port: Schema.number().default(18080),
  healthTimeoutMs: Schema.number().default(120_000),
})

export interface LegacyEngineService {
  /** 本地后端根地址，如 http://127.0.0.1:18080。 */
  baseUrl: string
  /** 最近约 200 行 stdout/stderr（alembic / uvicorn 的启动横幅都在这），排错用。 */
  logTail(): string
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
    '-e', 'POLARIS_PROFILE=desktop',
    '-e', 'POLARIS_LLM_FAKE_FALLBACK=1',
    config.image,
    'sh', '-lc',
    // 先迁移后起服务：desktop 档位没有独立 worker 抢跑迁移的问题，串行即可
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
        // 两种模式统一注入 desktop 档位环境。docker 模式下真正生效的是
        // 上面的 -e 参数（这里只影响 docker 客户端，无害）；command 模式
        // 靠它保证裸进程也跑在单进程档位上。
        env: { ...process.env, POLARIS_PROFILE: 'desktop', POLARIS_LLM_FAKE_FALLBACK: '1' },
      })
      child.stdout!.on('data', capture)
      child.stderr!.on('data', capture)
      return async () => {
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
        throw new Error(
          `legacy engine exited before becoming healthy (code=${child.exitCode}, signal=${child.signalCode})\n${lines.join('\n')}`,
        )
      }
      try {
        const res = await fetch(`${baseUrl}/api/health`)
        if (res.ok) break
      } catch {
        /* 还没起来 */
      }
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
    }
    ctx.provide('legacy', service)
  },
}
