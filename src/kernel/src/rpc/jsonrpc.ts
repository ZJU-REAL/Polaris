/* ============================================================
   Line-delimited JSON-RPC 2.0 endpoint over a stream pair.

   Promoted from src/desktop/src/main/agent/rpc.ts (the desktop shell keeps
   its copy until it mounts the kernel in P1). Framing stays aligned with
   src/backend/app/mcp/__main__.py: one JSON object per line, so a Python
   process can sit on the other end of the pipe unchanged.

   This module extends the desktop client with the pieces the kernel seams
   need: notifications (messages without an id) and inbound method calls
   (Python → Node), both optional. Backpressure and large-payload handling
   are Spike 3 scope and will land with benchmarks.
   ============================================================ */

import type { Readable, Writable } from 'node:stream'

interface Pending {
  resolve: (value: unknown) => void
  reject: (err: Error) => void
  timer: NodeJS.Timeout
}

export type RpcHandler = (params: unknown) => unknown | Promise<unknown>
export type NotificationHandler = (method: string, params: unknown) => void

export interface JsonRpcEndpointOptions {
  /** Timeout for outbound calls, in milliseconds. */
  timeoutMs?: number
  /** Called for inbound notifications (messages without an id). */
  onNotification?: NotificationHandler
}

export class JsonRpcEndpoint {
  private nextId = 1
  private readonly pending = new Map<number, Pending>()
  private readonly methods = new Map<string, RpcHandler>()
  private buffer = ''
  private readonly timeoutMs: number
  private readonly onNotification?: NotificationHandler

  constructor(
    private readonly output: Writable,
    input: Readable,
    options: JsonRpcEndpointOptions = {},
  ) {
    this.timeoutMs = options.timeoutMs ?? 30_000
    this.onNotification = options.onNotification
    input.setEncoding('utf8')
    input.on('data', (chunk: string) => this.onData(chunk))
  }

  /** Register a handler for inbound calls (the peer's `call` reaches here). */
  handle(method: string, handler: RpcHandler): () => void {
    this.methods.set(method, handler)
    return () => {
      if (this.methods.get(method) === handler) this.methods.delete(method)
    }
  }

  call(method: string, params?: unknown): Promise<unknown> {
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`rpc call timed out: ${method}`))
      }, this.timeoutMs)
      this.pending.set(id, { resolve, reject, timer })
      this.send({ jsonrpc: '2.0', id, method, params })
    })
  }

  /** Fire-and-forget notification (no id, no response expected). */
  notify(method: string, params?: unknown): void {
    this.send({ jsonrpc: '2.0', method, params })
  }

  /** Peer process died: reject every in-flight request deterministically. */
  rejectAll(reason: string): void {
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer)
      entry.reject(new Error(reason))
    }
    this.pending.clear()
  }

  private send(msg: Record<string, unknown>): void {
    this.output.write(`${JSON.stringify(msg)}\n`)
  }

  private onData(chunk: string): void {
    this.buffer += chunk
    let nl: number
    while ((nl = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, nl).trim()
      this.buffer = this.buffer.slice(nl + 1)
      if (!line) continue
      try {
        void this.dispatch(JSON.parse(line) as Record<string, unknown>)
      } catch {
        // Non-JSON on the pipe (usually a library logging to stdout) must
        // never poison the channel — ignore the line.
      }
    }
  }

  private async dispatch(msg: Record<string, unknown>): Promise<void> {
    const id = msg.id
    if (typeof msg.method === 'string') {
      // Inbound call or notification from the peer.
      if (typeof id !== 'number') {
        this.onNotification?.(msg.method, msg.params)
        return
      }
      const handler = this.methods.get(msg.method)
      if (!handler) {
        this.send({ jsonrpc: '2.0', id, error: { code: -32601, message: `method not found: ${msg.method}` } })
        return
      }
      try {
        const result = await handler(msg.params)
        this.send({ jsonrpc: '2.0', id, result: result ?? null })
      } catch (error) {
        this.send({ jsonrpc: '2.0', id, error: { code: -32000, message: (error as Error)?.message ?? 'handler failed' } })
      }
      return
    }
    // Response to one of our outbound calls.
    if (typeof id !== 'number') return
    const entry = this.pending.get(id)
    if (!entry) return
    this.pending.delete(id)
    clearTimeout(entry.timer)
    if (msg.error) {
      const err = msg.error as { code?: number; message?: string }
      entry.reject(new Error(err.message ?? `rpc error ${err.code ?? ''}`))
    } else {
      entry.resolve(msg.result)
    }
  }
}
