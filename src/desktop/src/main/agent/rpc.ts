/* ============================================================
   行分隔 JSON-RPC 2.0 客户端（main → 本地 agent）。

   framing 刻意与 src/backend/app/mcp/__main__.py 对齐：一行一个 JSON 对象。
   代价几乎为零，换来的是「将来真需要 Python 做本地解析/嵌入时，可以直接把一个
   python -m ... 进程挂到同一个 supervisor 下，main 与前端一行不改」。

   注意：renderer ↔ main 那一跳不用 JSON-RPC——ipcRenderer.invoke 已经有 Promise
   语义，再包一层是白付开销。JSON-RPC 只用在这条真正的跨进程管道上（需要 id 配对）。
   ============================================================ */

import type { Readable, Writable } from 'node:stream';

interface Pending {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
  timer: NodeJS.Timeout;
}

export class JsonRpcClient {
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private buffer = '';

  constructor(
    private readonly stdin: Writable,
    stdout: Readable,
    private readonly timeoutMs = 30_000,
  ) {
    stdout.setEncoding('utf8');
    stdout.on('data', (chunk: string) => this.onData(chunk));
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let nl: number;
    while ((nl = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, nl).trim();
      this.buffer = this.buffer.slice(nl + 1);
      if (!line) continue;
      try {
        this.dispatch(JSON.parse(line) as Record<string, unknown>);
      } catch {
        // agent 吐了非 JSON（多半是库往 stdout 打的日志）——忽略，不能让它毒死整条管道
      }
    }
  }

  private dispatch(msg: Record<string, unknown>): void {
    const id = msg.id;
    if (typeof id !== 'number') return; // 通知类消息，一期不用
    const entry = this.pending.get(id);
    if (!entry) return;
    this.pending.delete(id);
    clearTimeout(entry.timer);
    if (msg.error) {
      const err = msg.error as { code?: number; message?: string };
      entry.reject(new Error(err.message ?? `agent error ${err.code ?? ''}`));
    } else {
      entry.resolve(msg.result);
    }
  }

  call(method: string, params?: unknown): Promise<unknown> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`agent call timed out: ${method}`));
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    });
  }

  /** 进程没了：把所有在途请求一次性 reject，避免调用方永远挂着。 */
  rejectAll(reason: string): void {
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.reject(new Error(reason));
    }
    this.pending.clear();
  }
}
