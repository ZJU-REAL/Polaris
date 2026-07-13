/* ============================================================
   SSE 客户端 — fetch + ReadableStream 手写解析。
   原生 EventSource 不支持 Authorization header，故用 fetch 携带
   Bearer token；按 SSE 规范解析 event:/data:/注释行（心跳 ": ping"），
   连接断开后指数退避自动重连（成功收到响应后重置退避）。
   ============================================================ */

import { getToken } from './api';

export interface SseHandlers {
  /** 每收到一个完整事件调用（event 缺省为 "message"，data 为原始字符串）。 */
  onEvent: (event: string, data: string) => void;
  /** 每次成功建立流时调用。 */
  onOpen?: () => void;
  /** 每次连接失败/中断（将自动重连）时调用。 */
  onError?: (err: unknown) => void;
}

const MAX_BACKOFF_MS = 15_000;

/**
 * 订阅 `/api{path}` 的 SSE 流。返回取消函数（中止连接并停止重连）。
 */
export function subscribeSse(path: string, handlers: SseHandlers): () => void {
  let stopped = false;
  let ctrl = new AbortController();
  let attempt = 0;

  async function readStream(res: Response): Promise<void> {
    if (!res.body) throw new Error('SSE response has no body');
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let eventType = 'message';
    let dataLines: string[] = [];

    const dispatch = () => {
      if (dataLines.length > 0) {
        handlers.onEvent(eventType, dataLines.join('\n'));
      }
      eventType = 'message';
      dataLines = [];
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf('\n')) >= 0) {
        let line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (line.endsWith('\r')) line = line.slice(0, -1);
        if (line === '') {
          dispatch();
          continue;
        }
        if (line.startsWith(':')) continue; // 注释行 = 心跳，忽略
        const colon = line.indexOf(':');
        const field = colon === -1 ? line : line.slice(0, colon);
        let value2 = colon === -1 ? '' : line.slice(colon + 1);
        if (value2.startsWith(' ')) value2 = value2.slice(1);
        if (field === 'event') {
          eventType = value2;
        } else if (field === 'data') {
          dataLines.push(value2);
        }
        // id / retry 字段 M1 不用，忽略
      }
    }
    dispatch();
  }

  async function loop(): Promise<void> {
    while (!stopped) {
      ctrl = new AbortController();
      try {
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        const token = getToken();
        if (token) headers.Authorization = `Bearer ${token}`;
        const res = await fetch(`/api${path}`, { headers, signal: ctrl.signal });
        if (!res.ok) throw new Error(`SSE HTTP ${res.status}`);
        attempt = 0;
        handlers.onOpen?.();
        await readStream(res);
        // 服务端正常关闭（如 voyage 到终态）——仍走重连逻辑，由调用方在
        // 状态变为终态后取消订阅。
      } catch (err) {
        if (stopped || ctrl.signal.aborted) return;
        handlers.onError?.(err);
      }
      if (stopped) return;
      const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempt);
      attempt += 1;
      await new Promise((r) => setTimeout(r, delay));
    }
  }

  void loop();

  return () => {
    stopped = true;
    ctrl.abort();
  };
}
