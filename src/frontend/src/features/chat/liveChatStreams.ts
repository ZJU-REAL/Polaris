import { persistAssistantProgress } from './chatStorage';
import type { ChatMsg } from './types';

/* ============================================================
   在途对话流登记表（模块级，不随组件生死）。

   起因：ChatSurface 过去在卸载时直接 abort 掉 SSE。用户点开一条引用、
   切到文献窗口，对话就被掐断，回来只剩一条永远转圈的「正在思考…」。
   现在流登记在这里，按「会话」归属：组件卸载只是取消订阅，流照跑；
   无人订阅期间由本模块把进展写回 localStorage，切回来即可认领续看。
   ============================================================ */

export interface StreamSnapshot {
  content: string;
  sources?: ChatMsg['sources'];
  done: boolean;
  failed?: boolean;
}

type Subscriber = (snap: StreamSnapshot) => void;

interface Entry extends StreamSnapshot {
  surfaceKey: string;
  convId: string;
  stop: () => void;
  subs: Set<Subscriber>;
  /** 无人订阅时的落盘节流句柄 */
  saveTimer: number | null;
}

const SAVE_THROTTLE_MS = 400;
/** 结束后保留一小会儿，好让「切回来」还能认领到最终态；之后由 localStorage 兜底 */
const KEEP_AFTER_DONE_MS = 60_000;

const entries = new Map<string, Entry>();

export function streamKey(surfaceKey: string, convId: string): string {
  return `${surfaceKey}::${convId}`;
}

function snapshot(e: Entry): StreamSnapshot {
  return { content: e.content, sources: e.sources, done: e.done, failed: e.failed };
}

function flush(e: Entry) {
  if (e.saveTimer != null) {
    clearTimeout(e.saveTimer);
    e.saveTimer = null;
  }
  persistAssistantProgress(e.surfaceKey, e.convId, {
    content: e.content,
    ...(e.sources ? { sources: e.sources } : {}),
    ...(e.done ? { done: true } : {}),
    ...(e.failed ? { failed: true } : {}),
  });
}

function publish(e: Entry) {
  if (e.subs.size > 0) {
    // 有人挂载着：由订阅方 commit（同时更新 React 状态和 localStorage），
    // 这里不再自己写盘，免得两边互相覆盖
    const snap = snapshot(e);
    e.subs.forEach((fn) => fn(snap));
    return;
  }
  if (e.done) {
    flush(e);
    return;
  }
  if (e.saveTimer == null) {
    e.saveTimer = window.setTimeout(() => {
      e.saveTimer = null;
      if (e.subs.size === 0) flush(e);
    }, SAVE_THROTTLE_MS);
  }
}

export interface StreamHandlers {
  onDelta: (text: string) => void;
  onSources?: (dataStr: string) => void;
  onDone: () => void;
  onError: (detail: string) => void;
}

/**
 * 起一条流并登记。``begin`` 收到内部 handlers，返回中止函数。
 * ``onFail`` 在出错时调用一次（用于弹提示），组件是否还挂着都会触发。
 */
export function startStream(
  key: string,
  surfaceKey: string,
  convId: string,
  begin: (handlers: StreamHandlers) => () => void,
  onFail?: (detail: string) => void,
): void {
  entries.get(key)?.stop();
  const e: Entry = {
    surfaceKey,
    convId,
    content: '',
    done: false,
    subs: new Set(),
    saveTimer: null,
    stop: () => {},
  };
  entries.set(key, e);

  const finish = (failed: boolean, fallback?: string) => {
    if (e.done) return;
    e.done = true;
    if (failed) {
      e.failed = true;
      if (!e.content && fallback) e.content = fallback;
    }
    publish(e);
    window.setTimeout(() => {
      if (entries.get(key) === e) entries.delete(key);
    }, KEEP_AFTER_DONE_MS);
  };

  e.stop = begin({
    onDelta: (text) => {
      if (!text || e.done) return;
      e.content += text;
      publish(e);
    },
    onSources: (dataStr) => {
      if (e.done) return;
      try {
        e.sources = (JSON.parse(dataStr) as { items?: ChatMsg['sources'] }).items ?? [];
      } catch {
        return;
      }
      publish(e);
    },
    onDone: () => finish(false),
    onError: (detail) => {
      onFail?.(detail);
      finish(true, '（回答中断了，请重试）');
    },
  });
}

/** 订阅某条在途流；返回退订函数。订阅时立即推一次当前快照。 */
export function subscribeStream(key: string, fn: Subscriber): () => void {
  const e = entries.get(key);
  if (!e) return () => {};
  e.subs.add(fn);
  fn(snapshot(e));
  return () => {
    e.subs.delete(fn);
    // 最后一个订阅者走了（组件卸载）：立刻把当前进展落盘，之后由本模块接管
    if (e.subs.size === 0) flush(e);
  };
}

/** 该会话是否有还没结束的流（切回来时据此恢复「正在生成」状态）。 */
export function isStreaming(key: string): boolean {
  const e = entries.get(key);
  return !!e && !e.done;
}

/** 用户主动停止：中止连接并按已生成内容收尾。 */
export function stopStream(key: string): void {
  const e = entries.get(key);
  if (!e || e.done) return;
  e.stop();
  e.done = true;
  publish(e);
  entries.delete(key);
}
