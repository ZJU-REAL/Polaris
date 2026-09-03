/* legacy-engine 插件的单元测试：只测 command 模式，故意不碰 docker——
   docker 形态由 desktop 冒烟（POLARIS_SMOKE_ENGINE=1）覆盖，这里保证
   在任何 CI 机器上都能跑。假引擎用 node -e 起一个最小 HTTP 服务器。 */
import { describe, expect, it } from 'vitest'
import { createKernel, legacyEngine, type LegacyEngineService } from '../src/index.ts'

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
    // 顺带回显 profile：断言 command 模式也注入了 POLARIS_PROFILE=desktop
    "  res.end(JSON.stringify({ status: 'ok', profile: process.env.POLARIS_PROFILE || '' }));",
    '});',
    `srv.listen(${port}, '127.0.0.1');`,
  ].join('\n')
}

describe('legacy-engine plugin (command mode)', () => {
  it('provides the legacy service once healthy and kills the process on dispose', async () => {
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
    const body = (await res.json()) as { status: string; profile: string }
    expect(body.status).toBe('ok')
    expect(body.profile).toBe('desktop')

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
