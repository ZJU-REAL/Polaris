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
  key: string;
  surfaceKey: string;
  convId: string;
  stop: () => void;
  /** 无人订阅时的落盘节流句柄 */
  saveTimer: number | null;
}

const SAVE_THROTTLE_MS = 400;
/** 结束后保留一小会儿，好让「切回来」还能认领到最终态；之后由 localStorage 兜底 */
const KEEP_AFTER_DONE_MS = 60_000;

const entries = new Map<string, Entry>();

/* 订阅者按 key 单独登记，**不挂在 Entry 上**。

   挂在 Entry 上时，只要那条 Entry 被换掉或删掉，订阅关系就随之消失，而组件那边
   activeKey 没变、订阅 effect 不会重跑，于是它再也收不到推送——界面一直转圈，可流
   其实在跑。切走再切回来（组件重挂）才重新订阅，这才看见回答正在流式输出。

   三条路径都会踩到：追问时 startStream 换掉同 key 的 Entry；用户点停止后 stopStream
   删掉 Entry；以及一轮结束 KEEP_AFTER_DONE_MS 到点删掉 Entry 之后再提问。 */
const subscribers = new Map<string, Set<Subscriber>>();

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
  const subs = subscribers.get(e.key);
  if (subs && subs.size > 0) {
    // 有人挂载着：由订阅方 commit（同时更新 React 状态和 localStorage），
    // 这里不再自己写盘，免得两边互相覆盖
    const snap = snapshot(e);
    subs.forEach((fn) => fn(snap));
    return;
  }
  if (e.done) {
    flush(e);
    return;
  }
  if (e.saveTimer == null) {
    e.saveTimer = window.setTimeout(() => {
      e.saveTimer = null;
      if (!subscribers.get(e.key)?.size) flush(e);
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
    key,
    surfaceKey,
    convId,
    content: '',
    done: false,
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

/** 订阅某个会话的流；返回退订函数。已有在途流时立即推一次当前快照。

    **订阅不要求流已经存在**：组件挂在一个还没提问的会话上时也照样登记，这样之后
    startStream 起流时它立刻就能收到推送。以前这里 `if (!e) return () => {}` 直接
    放弃订阅，是「一直转圈」的另一半原因。 */
export function subscribeStream(key: string, fn: Subscriber): () => void {
  let subs = subscribers.get(key);
  if (!subs) {
    subs = new Set();
    subscribers.set(key, subs);
  }
  subs.add(fn);
  const e = entries.get(key);
  if (e) fn(snapshot(e));
  return () => {
    const cur = subscribers.get(key);
    if (!cur) return;
    cur.delete(fn);
    if (cur.size > 0) return;
    subscribers.delete(key);
    // 最后一个订阅者走了（组件卸载）：立刻把当前进展落盘，之后由本模块接管
    const live = entries.get(key);
    if (live) flush(live);
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
