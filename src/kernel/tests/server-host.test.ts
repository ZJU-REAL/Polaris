/* 服务器形态的内核宿主（#754）：裸 Node 起内核 + HTTP JSON-RPC。
   桌面那侧由 desktop smoke 把关，这里把同一批方法放到 HTTP 上再走一遍，
   重点是传输与信任边界——桌面永远测不到这两样。 */

import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { SERVER_SEED_ENTRIES, startServerKernel } from '../src/server/main.ts'

let dataDir: string
let started: Awaited<ReturnType<typeof startServerKernel>> | null = null
const TOKEN = 'test-token-0123456789'

async function rpc(port: number, method: string, params?: unknown, token = TOKEN) {
  const res = await fetch(`http://127.0.0.1:${port}/rpc`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-polaris-kernel-token': token },
    body: JSON.stringify({ method, params }),
  })
  return { status: res.status, body: (await res.json()) as { result?: unknown; error?: string } }
}

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'polaris-kernel-server-'))
})

afterEach(async () => {
  if (started) {
    await started.rpc.close()
    await started.host.kernel.stop()
    started = null
  }
  rmSync(dataDir, { recursive: true, force: true })
})

describe('server kernel host', () => {
  it('boots the plugin tree and answers plugins.list over HTTP', async () => {
    started = await startServerKernel({ dataDir, token: TOKEN, port: 0, host: '127.0.0.1' })

    const { status, body } = await rpc(started.rpc.port, 'plugins.list')
    expect(status).toBe(200)
    const entries = body.result as { id: string; state: string }[]
    // 服务器种子树只有 sources：legacy-engine 归 compose 管，不该由内核再拉一份
    expect(entries.map((e) => e.id)).toEqual(['sources'])
    expect(SERVER_SEED_ENTRIES.map((e) => e.id)).toEqual(['sources'])
  })

  it('refuses a caller without the shared token', async () => {
    started = await startServerKernel({ dataDir, token: TOKEN, port: 0, host: '127.0.0.1' })

    const { status, body } = await rpc(started.rpc.port, 'plugins.list', undefined, 'wrong-token')
    expect(status).toBe(401)
    expect(body.result).toBeUndefined()
    // 不解释缺了什么：这条边界后面是插件安装
    expect(body.error).toBe('unauthorized')
  })

  it('refuses to start without a token rather than running unauthenticated', async () => {
    await expect(
      startServerKernel({ dataDir, token: '', port: 0, host: '127.0.0.1' }),
    ).rejects.toThrow(/token is required/)
  })

  it('reports an unknown method instead of hanging', async () => {
    started = await startServerKernel({ dataDir, token: TOKEN, port: 0, host: '127.0.0.1' })

    const { status, body } = await rpc(started.rpc.port, 'plugins.nope')
    expect(status).toBe(404)
    expect(body.error).toMatch(/ERR_UNKNOWN_METHOD/)
  })

  it('returns method failures as a result, not as a transport error', async () => {
    started = await startServerKernel({ dataDir, token: TOKEN, port: 0, host: '127.0.0.1' })

    // 形状守卫：id 必须是字符串。这属于「这次调用不对」，不是「服务挂了」
    const { status, body } = await rpc(started.rpc.port, 'plugins.enable', { id: 42 })
    expect(status).toBe(400)
    expect(body.error).toMatch(/ERR_INVALID_PARAMS/)
  })

  it('persists the tree across restarts of the host', async () => {
    started = await startServerKernel({ dataDir, token: TOKEN, port: 0, host: '127.0.0.1' })
    await rpc(started.rpc.port, 'plugins.disable', { id: 'sources' })
    await started.rpc.close()
    await started.host.kernel.stop()

    started = await startServerKernel({ dataDir, token: TOKEN, port: 0, host: '127.0.0.1' })
    const { body } = await rpc(started.rpc.port, 'plugins.list')
    const entries = body.result as { id: string; state: string }[]
    // 用户意图（禁用）必须活过重启，否则树就不是真相了
    expect(entries.find((e) => e.id === 'sources')?.state).toBe('disabled')
  })

  it('streams job events to an SSE subscriber', async () => {
    started = await startServerKernel({ dataDir, token: TOKEN, port: 0, host: '127.0.0.1' })

    const controller = new AbortController()
    const res = await fetch(`http://127.0.0.1:${started.rpc.port}/events`, {
      headers: { 'x-polaris-kernel-token': TOKEN },
      signal: controller.signal,
    })
    expect(res.status).toBe(200)
    const reader = res.body!.getReader()
    await reader.read() // 先吃掉握手注释帧

    const jobId = started.jobs.start('test')
    started.jobs.progress(jobId, 'download', 1, 4)

    const chunk = await reader.read()
    const text = new TextDecoder().decode(chunk.value)
    expect(text).toContain('job.progress')
    expect(text).toContain('download')
    controller.abort()
  })
})
