/* ============================================================
   服务器形态的 RPC 传输（#754）：JSON-RPC over HTTP + SSE 事件流。

   桌面用 Electron IPC，服务器用这一层，两边调用的是同一批方法
   （createPluginMethods / createMarketMethods）。

   ## 信任边界

   本服务**不做用户认证**，只认一个共享密钥，并且只监听回环/内网地址。
   用户身份归 Python 后端：浏览器带着会话打后端，后端用既有的 owner 守卫
   （#738）判权限，通过后才代理到这里。

   为什么不在这里自己校验 JWT：那等于把认证实现成两份。两份认证迟早会在
   「谁算 owner」这种问题上分叉，而分叉的那一半就是漏洞。这里只回答
   「调用方是不是我信任的那个后端」——一个密钥足够，也只需要这么多。

   所以部署上有一条硬要求：**这个端口绝不能对外暴露**。compose 里它不发布
   端口，只挂在内部网络上；密钥缺失时直接拒绝启动，而不是退化成不校验。
   ============================================================ */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { timingSafeEqual } from 'node:crypto'

import type { JobEvent } from '../rpc/jobs.ts'
import type { RpcMethod } from '../rpc/plugin-methods.ts'

/** 单个请求体上限：RPC 参数都是短标识符，树导入是最大的一个。 */
const MAX_BODY_BYTES = 1_000_000

export interface RpcHttpServerOptions {
  methods: Record<string, RpcMethod>
  /** 共享密钥，来自 env；空值视为配置错误，调用方应拒绝启动。 */
  token: string
  /** 绑定地址。默认只听回环——容器里要跨服务访问时才传 0.0.0.0。 */
  host?: string
  port: number
  log?: { warn(message: string): void; error(message: string, error?: unknown): void }
}

export interface RpcHttpServer {
  /** 实际监听的端口（传 0 时由内核分配，测试用）。 */
  readonly port: number
  /** 把一条事件推给所有 SSE 订阅者。 */
  broadcast(event: JobEvent): void
  close(): Promise<void>
}

function timingSafeEqualStr(a: string, b: string): boolean {
  const left = Buffer.from(a)
  const right = Buffer.from(b)
  // 长度不同时 timingSafeEqual 会抛错；先比长度会泄露长度，但密钥长度不是秘密
  if (left.length !== right.length) return false
  return timingSafeEqual(left, right)
}

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = []
  let size = 0
  for await (const chunk of req) {
    size += (chunk as Buffer).length
    if (size > MAX_BODY_BYTES) throw new Error('request body too large')
    chunks.push(chunk as Buffer)
  }
  return Buffer.concat(chunks).toString('utf8')
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body)
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' })
  res.end(text)
}

export function createRpcHttpServer(options: RpcHttpServerOptions): Promise<RpcHttpServer> {
  const log = options.log ?? console
  if (!options.token) {
    // 没有密钥就不是「宽松模式」，是配置错误。宁可起不来也不要裸奔的内核。
    throw new Error('rpc-http: a shared token is required')
  }

  const subscribers = new Set<ServerResponse>()

  function authorized(req: IncomingMessage): boolean {
    const header = req.headers['x-polaris-kernel-token']
    const value = Array.isArray(header) ? header[0] : header
    return typeof value === 'string' && timingSafeEqualStr(value, options.token)
  }

  const server: Server = createServer((req, res) => {
    void (async () => {
      if (!authorized(req)) {
        // 不解释缺了什么：这条边界后面是插件安装，能少说就少说
        sendJson(res, 401, { error: 'unauthorized' })
        return
      }

      if (req.method === 'GET' && req.url === '/events') {
        res.writeHead(200, {
          'content-type': 'text/event-stream; charset=utf-8',
          'cache-control': 'no-cache',
          connection: 'keep-alive',
        })
        res.write(': connected\n\n')
        subscribers.add(res)
        req.on('close', () => subscribers.delete(res))
        return
      }

      if (req.method !== 'POST' || req.url !== '/rpc') {
        sendJson(res, 404, { error: 'not found' })
        return
      }

      let payload: { method?: unknown; params?: unknown }
      try {
        payload = JSON.parse(await readBody(req)) as typeof payload
      } catch (err) {
        sendJson(res, 400, { error: `invalid json: ${err instanceof Error ? err.message : err}` })
        return
      }

      const method = typeof payload.method === 'string' ? payload.method : ''
      const handler = options.methods[method]
      if (!handler) {
        sendJson(res, 404, { error: `ERR_UNKNOWN_METHOD: ${method}` })
        return
      }

      try {
        const result = await handler(payload.params)
        sendJson(res, 200, { result: result ?? null })
      } catch (err) {
        // 方法层的错误是**业务结果**，不是传输故障：用 200 之外的码会让
        // 代理层和前端把「这个插件装不了」当成服务挂了。给 400 + 结构化消息。
        const message = err instanceof Error ? err.message : String(err)
        log.warn(`[rpc-http] ${method} failed: ${message}`)
        sendJson(res, 400, { error: message })
      }
    })()
  })

  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(options.port, options.host ?? '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : options.port
      resolve({
        port,
        broadcast(event: JobEvent) {
          const frame = `data: ${JSON.stringify(event)}\n\n`
          for (const res of subscribers) {
            try {
              res.write(frame)
            } catch {
              subscribers.delete(res)
            }
          }
        },
        close: () =>
          new Promise<void>((done) => {
            for (const res of subscribers) res.end()
            subscribers.clear()
            server.close(() => done())
          }),
      })
    })
  })
}
