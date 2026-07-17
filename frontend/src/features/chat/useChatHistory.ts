import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ChatMsg, Conversation } from './types';

/* ============================================================
   本地会话历史：按 surfaceKey 命名空间存 localStorage。
   后端暂无会话持久化，这里先用本地存储把「历史对话」体验搭起来。
   ============================================================ */

const NS = 'polaris.chat';
const MAX_CONVERSATIONS = 40;

function key(surfaceKey: string): string {
  return `${NS}:${surfaceKey}`;
}

function load(surfaceKey: string): Conversation[] {
  try {
    const raw = localStorage.getItem(key(surfaceKey));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Conversation[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function save(surfaceKey: string, list: Conversation[]) {
  try {
    localStorage.setItem(key(surfaceKey), JSON.stringify(list.slice(0, MAX_CONVERSATIONS)));
  } catch {
    /* 配额/隐私模式：静默降级为仅本次会话 */
  }
}

function newId(): string {
  return `c_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

/** 首条用户消息 → 会话标题（截断） */
function titleFromMsgs(msgs: ChatMsg[]): string {
  const first = msgs.find((m) => m.role === 'user' && m.content.trim());
  const t = first?.content.trim() ?? '';
  return t ? (t.length > 40 ? `${t.slice(0, 40)}…` : t) : '';
}

export interface ChatHistory {
  conversations: Conversation[];
  activeId: string | null;
  activeMsgs: ChatMsg[];
  /** 用最新消息覆盖当前会话（无会话时按需新建） */
  commit: (msgs: ChatMsg[]) => void;
  select: (id: string) => void;
  create: () => void;
  remove: (id: string) => void;
}

export function useChatHistory(surfaceKey: string): ChatHistory {
  const [conversations, setConversations] = useState<Conversation[]>(() => load(surfaceKey));
  const [activeId, setActiveId] = useState<string | null>(() => load(surfaceKey)[0]?.id ?? null);

  // surfaceKey 变了（切项目/换论文）→ 重新载入该命名空间
  const skRef = useRef(surfaceKey);
  useEffect(() => {
    if (skRef.current === surfaceKey) return;
    skRef.current = surfaceKey;
    const list = load(surfaceKey);
    setConversations(list);
    setActiveId(list[0]?.id ?? null);
  }, [surfaceKey]);

  const activeMsgs = useMemo(
    () => conversations.find((c) => c.id === activeId)?.msgs ?? [],
    [conversations, activeId],
  );

  const commit = useCallback(
    (msgs: ChatMsg[]) => {
      setConversations((prev) => {
        let id = activeId;
        let list = prev;
        if (!id || !prev.some((c) => c.id === id)) {
          id = newId();
          setActiveId(id);
          list = [{ id, title: '', msgs: [], updatedAt: Date.now() }, ...prev];
        }
        const next = list.map((c) =>
          c.id === id
            ? { ...c, msgs, title: c.title || titleFromMsgs(msgs), updatedAt: Date.now() }
            : c,
        );
        // 活跃会话置顶
        next.sort((a, b) => b.updatedAt - a.updatedAt);
        save(surfaceKey, next);
        return next;
      });
    },
    [activeId, surfaceKey],
  );

  const select = useCallback((id: string) => setActiveId(id), []);

  const create = useCallback(() => {
    // 只有当前会话非空时才真正开新的；空会话直接复用
    setConversations((prev) => {
      const active = prev.find((c) => c.id === activeId);
      if (active && active.msgs.length === 0) return prev;
      const id = newId();
      setActiveId(id);
      const next = [{ id, title: '', msgs: [], updatedAt: Date.now() }, ...prev];
      save(surfaceKey, next);
      return next;
    });
  }, [activeId, surfaceKey]);

  const remove = useCallback(
    (id: string) => {
      setConversations((prev) => {
        const next = prev.filter((c) => c.id !== id);
        save(surfaceKey, next);
        if (id === activeId) setActiveId(next[0]?.id ?? null);
        return next;
      });
    },
    [activeId],
  );

  return { conversations, activeId, activeMsgs, commit, select, create, remove };
}
