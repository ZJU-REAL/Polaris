/* ============================================================
   WebSocket 通知客户端 — /ws/notifications?token=<jwt>
   断线指数退避重连；服务端推送 gate.created / gate.decided /
   voyage.status（见 docs/api-m1.md §5）。
   ============================================================ */

import type { GateRead } from './api';

export type NotificationMessage =
  | { type: 'gate.created'; gate: GateRead }
  | { type: 'gate.decided'; gate: GateRead }
  | { type: 'voyage.status'; voyage_id: string; status: string };

const MAX_BACKOFF_MS = 30_000;

/**
 * 建立通知 WS 连接。返回关闭函数（停止重连并断开）。
 * token 通过 getter 传入，重连时取最新值。
 */
export function connectNotifications(
  getTokenFn: () => string | null,
  onMessage: (msg: NotificationMessage) => void,
): () => void {
  let closed = false;
  let ws: WebSocket | null = null;
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  function scheduleReconnect() {
    if (closed) return;
    const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempt);
    attempt += 1;
    timer = setTimeout(open, delay);
  }

  function open() {
    if (closed) return;
    const token = getTokenFn();
    if (!token) {
      scheduleReconnect();
      return;
    }
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${window.location.host}/ws/notifications?token=${encodeURIComponent(token)}`;
    try {
      ws = new WebSocket(url);
    } catch {
      scheduleReconnect();
      return;
    }
    ws.onopen = () => {
      attempt = 0;
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(String(e.data)) as NotificationMessage;
        if (msg && typeof msg === 'object' && 'type' in msg) onMessage(msg);
      } catch {
        /* 忽略非 JSON 消息 */
      }
    };
    ws.onclose = () => {
      ws = null;
      scheduleReconnect();
    };
    ws.onerror = () => {
      ws?.close();
    };
  }

  open();

  return () => {
    closed = true;
    if (timer !== undefined) clearTimeout(timer);
    ws?.close();
    ws = null;
  };
}
