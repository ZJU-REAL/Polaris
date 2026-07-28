import type { ChatMsg, Conversation } from './types';

/* ============================================================
   对话本地存储：按 surfaceKey 命名空间存 localStorage。
   与 React 无关，故单独成文件——在途流在组件卸载后也要往这里写。
   ============================================================ */

const NS = 'polaris.chat';
export const MAX_CONVERSATIONS = 40;

function key(surfaceKey: string): string {
  return `${NS}:${surfaceKey}`;
}

export function load(surfaceKey: string): Conversation[] {
  try {
    const raw = localStorage.getItem(key(surfaceKey));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Conversation[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function save(surfaceKey: string, list: Conversation[]) {
  try {
    localStorage.setItem(key(surfaceKey), JSON.stringify(list.slice(0, MAX_CONVERSATIONS)));
  } catch {
    /* 配额/隐私模式：静默降级为仅本次会话 */
  }
}

export function newId(): string {
  return `c_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

/** 组件已卸载、没人接管渲染时，把流式进展直接写回 localStorage。
 *  只在无订阅者时调用（见 liveChatStreams），避免和挂载中的 commit 抢写。 */
export function persistAssistantProgress(
  surfaceKey: string,
  convId: string,
  patch: Partial<ChatMsg>,
) {
  const list = load(surfaceKey);
  const conv = list.find((c) => c.id === convId);
  if (!conv) return;
  const last = conv.msgs[conv.msgs.length - 1];
  if (!last || last.role !== 'assistant') return;
  conv.msgs = [...conv.msgs.slice(0, -1), { ...last, ...patch }];
  conv.updatedAt = Date.now();
  list.sort((a, b) => b.updatedAt - a.updatedAt);
  save(surfaceKey, list);
}
