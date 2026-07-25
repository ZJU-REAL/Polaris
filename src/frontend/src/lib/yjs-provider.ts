/* ============================================================
   轻量 Yjs WebSocket provider — 自写传输层（不依赖 y-websocket）。
   连 `WS /ws/manuscripts/{fid}?token=`（后端 pycrdt.websocket YRoom，
   房间名 = 稿件文件 id），编解码直接复用 y-protocols：
     - message type 0 = sync（SyncStep1 / SyncStep2 / Update）
     - message type 1 = awareness（协作者光标 / 在线状态）
   断线指数退避重连（同 lib/ws.ts）；离线期间的本地改动留在
   Y.Doc 内，重连后经 SyncStep1/2 全量对账自动补同步。
   ============================================================ */

import * as Y from 'yjs';
import * as encoding from 'lib0/encoding';
import * as decoding from 'lib0/decoding';
import * as syncProtocol from 'y-protocols/sync';
import * as awarenessProtocol from 'y-protocols/awareness';
import { wsUrl } from './endpoint';

const MSG_SYNC = 0;
const MSG_AWARENESS = 1;
const MSG_QUERY_AWARENESS = 3;

const MAX_BACKOFF_MS = 30_000;

export type ProviderStatus = 'connecting' | 'connected' | 'disconnected';

export class ManuscriptProvider {
  readonly doc: Y.Doc;
  readonly awareness: awarenessProtocol.Awareness;
  /** 是否已与服务器完成一轮 SyncStep1/2 对账。 */
  synced = false;
  status: ProviderStatus = 'connecting';

  private ws: WebSocket | null = null;
  private destroyed = false;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private statusListeners = new Set<(s: ProviderStatus) => void>();

  constructor(
    private readonly fileId: string,
    private readonly getTokenFn: () => string | null,
    doc?: Y.Doc,
  ) {
    this.doc = doc ?? new Y.Doc();
    this.awareness = new awarenessProtocol.Awareness(this.doc);
    this.doc.on('update', this.handleDocUpdate);
    this.awareness.on('update', this.handleAwarenessUpdate);
    this.open();
  }

  /** 订阅连接状态（立即回调一次当前值），返回退订函数。 */
  onStatus(cb: (s: ProviderStatus) => void): () => void {
    this.statusListeners.add(cb);
    cb(this.status);
    return () => this.statusListeners.delete(cb);
  }

  destroy(): void {
    this.destroyed = true;
    if (this.timer !== undefined) clearTimeout(this.timer);
    // 先广播离线（此时 update handler 还挂着，连接在时能送达），再解绑
    awarenessProtocol.removeAwarenessStates(this.awareness, [this.doc.clientID], 'destroy');
    this.doc.off('update', this.handleDocUpdate);
    this.awareness.off('update', this.handleAwarenessUpdate);
    this.awareness.destroy();
    this.ws?.close();
    this.ws = null;
    this.statusListeners.clear();
  }

  // ---------------- 内部 ----------------

  private setStatus(s: ProviderStatus): void {
    if (this.status === s) return;
    this.status = s;
    for (const cb of this.statusListeners) cb(s);
  }

  private send(buf: Uint8Array): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(buf);
    }
  }

  /** 本地 Y.Doc 更新 → 广播 Update（origin 为自己的远端应用不回发）。 */
  private handleDocUpdate = (update: Uint8Array, origin: unknown): void => {
    if (origin === this) return;
    const enc = encoding.createEncoder();
    encoding.writeVarUint(enc, MSG_SYNC);
    syncProtocol.writeUpdate(enc, update);
    this.send(encoding.toUint8Array(enc));
  };

  /** 本地 awareness 变化 → 广播（含清空 = removed）。 */
  private handleAwarenessUpdate = (
    { added, updated, removed }: { added: number[]; updated: number[]; removed: number[] },
    origin: unknown,
  ): void => {
    if (origin === this) return; // 远端应用回来的不再回发
    const changed = added.concat(updated, removed);
    const enc = encoding.createEncoder();
    encoding.writeVarUint(enc, MSG_AWARENESS);
    encoding.writeVarUint8Array(enc, awarenessProtocol.encodeAwarenessUpdate(this.awareness, changed));
    this.send(encoding.toUint8Array(enc));
  };

  private scheduleReconnect(): void {
    if (this.destroyed) return;
    const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** this.attempt);
    this.attempt += 1;
    this.timer = setTimeout(() => this.open(), delay);
  }

  private open(): void {
    if (this.destroyed) return;
    const token = this.getTokenFn();
    if (!token) {
      this.setStatus('disconnected');
      this.scheduleReconnect();
      return;
    }
    this.setStatus(this.attempt === 0 ? 'connecting' : this.status);
    const url = wsUrl(
      `/ws/manuscripts/${encodeURIComponent(this.fileId)}?token=${encodeURIComponent(token)}`,
    );
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      this.setStatus('disconnected');
      this.scheduleReconnect();
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;

    ws.onopen = () => {
      if (this.destroyed) return;
      this.attempt = 0;
      this.setStatus('connected');
      // 握手：发 SyncStep1（服务器回 Step2 并附上它的 Step1）
      const enc = encoding.createEncoder();
      encoding.writeVarUint(enc, MSG_SYNC);
      syncProtocol.writeSyncStep1(enc, this.doc);
      ws.send(encoding.toUint8Array(enc));
      // 把本地 awareness（光标/用户名）告知房间
      if (this.awareness.getLocalState() !== null) {
        const aenc = encoding.createEncoder();
        encoding.writeVarUint(aenc, MSG_AWARENESS);
        encoding.writeVarUint8Array(
          aenc,
          awarenessProtocol.encodeAwarenessUpdate(this.awareness, [this.doc.clientID]),
        );
        ws.send(encoding.toUint8Array(aenc));
      }
    };

    ws.onmessage = (e: MessageEvent) => {
      if (this.destroyed || !(e.data instanceof ArrayBuffer)) return;
      try {
        const dec = decoding.createDecoder(new Uint8Array(e.data));
        const msgType = decoding.readVarUint(dec);
        if (msgType === MSG_SYNC) {
          const enc = encoding.createEncoder();
          encoding.writeVarUint(enc, MSG_SYNC);
          const syncType = syncProtocol.readSyncMessage(dec, enc, this.doc, this);
          // readSyncMessage 对 Step1 会在 enc 里写好 Step2 应答
          if (encoding.length(enc) > 1) this.send(encoding.toUint8Array(enc));
          if (syncType === syncProtocol.messageYjsSyncStep2) this.synced = true;
        } else if (msgType === MSG_AWARENESS) {
          awarenessProtocol.applyAwarenessUpdate(this.awareness, decoding.readVarUint8Array(dec), this);
        } else if (msgType === MSG_QUERY_AWARENESS) {
          // 对端询问在线状态 → 回报本地 awareness
          if (this.awareness.getLocalState() !== null) {
            const enc = encoding.createEncoder();
            encoding.writeVarUint(enc, MSG_AWARENESS);
            encoding.writeVarUint8Array(
              enc,
              awarenessProtocol.encodeAwarenessUpdate(this.awareness, [this.doc.clientID]),
            );
            this.send(encoding.toUint8Array(enc));
          }
        }
      } catch {
        /* 忽略无法解析的消息 */
      }
    };

    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      this.synced = false;
      if (this.destroyed) return;
      this.setStatus('disconnected');
      // 房间里其他人视为离线（本地移除远端 awareness，避免残影光标）
      const remote = Array.from(this.awareness.getStates().keys()).filter(
        (c) => c !== this.doc.clientID,
      );
      awarenessProtocol.removeAwarenessStates(this.awareness, remote, this);
      this.scheduleReconnect();
    };

    ws.onerror = () => {
      ws.close();
    };
  }
}
