import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ChatMsg, Conversation } from './types';

/* ============================================================
   本地会话历史：按 surfaceKey 命名空间存 localStorage。
   后端暂无会话持久化，这里先用本地存储把「历史对话」体验搭起来。
   ============================================================ */

const NS = 'polaris.chat';
const MAX_CONVERSATIONS = 40;
const SAVE_THROTTLE_MS = 400; // 流式期间 localStorage 落盘节流间隔

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

  // commit 在流式回调闭包里被反复调用；用 ref 读当前 activeId，避免闭包捕获的旧 activeId（=null）
  // 导致每个 token 都新建一个会话。ref 与 state 保持同步（下面 effect），新建时同步写入。
  const activeIdRef = useRef<string | null>(activeId);
  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);

  // 持久化节流：流式期间每个 token 都 JSON.stringify + setItem 会卡顿；
  // state 仍每 token 更新（实时渲染），localStorage 最多每 SAVE_THROTTLE_MS 落一次盘。
  const saveTimer = useRef<number | null>(null);
  const pendingSave = useRef<{ sk: string; list: Conversation[] } | null>(null);
  const flushSave = useCallback(() => {
    if (saveTimer.current != null) {
      clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    const p = pendingSave.current;
    if (p) {
      pendingSave.current = null;
      save(p.sk, p.list);
    }
  }, []);
  const scheduleSave = useCallback(
    (sk: string, list: Conversation[]) => {
      pendingSave.current = { sk, list };
      if (saveTimer.current == null) {
        saveTimer.current = window.setTimeout(() => {
          saveTimer.current = null;
          flushSave();
        }, SAVE_THROTTLE_MS);
      }
    },
    [flushSave],
  );
  // 卸载时把未落盘的改动补写（避免流式刚结束就切走丢最后一段）
  useEffect(() => () => flushSave(), [flushSave]);

  // surfaceKey 变了（切项目/换论文）→ 先把上一个 surface 未落盘的补写，再重新载入该命名空间
  const skRef = useRef(surfaceKey);
  useEffect(() => {
    if (skRef.current === surfaceKey) return;
    flushSave();
    skRef.current = surfaceKey;
    const list = load(surfaceKey);
    setConversations(list);
    const nid = list[0]?.id ?? null;
    setActiveId(nid);
    activeIdRef.current = nid;
  }, [surfaceKey, flushSave]);

  const activeMsgs = useMemo(
    () => conversations.find((c) => c.id === activeId)?.msgs ?? [],
    [conversations, activeId],
  );

  const commit = useCallback(
    (msgs: ChatMsg[]) => {
      setConversations((prev) => {
        let id = activeIdRef.current;
        let list = prev;
        if (!id || !prev.some((c) => c.id === id)) {
          id = newId();
          activeIdRef.current = id; // 同步更新，后续 token 命中同一会话，不再重复新建
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
        scheduleSave(surfaceKey, next);
        return next;
      });
    },
    [surfaceKey, scheduleSave],
  );

  const select = useCallback((id: string) => setActiveId(id), []);

  const create = useCallback(() => {
    // 只有当前会话非空时才真正开新的；空会话直接复用
    setConversations((prev) => {
      const active = prev.find((c) => c.id === activeIdRef.current);
      if (active && active.msgs.length === 0) return prev;
      const id = newId();
      activeIdRef.current = id;
      setActiveId(id);
      const next = [{ id, title: '', msgs: [], updatedAt: Date.now() }, ...prev];
      flushSave();
      save(surfaceKey, next);
      return next;
    });
  }, [surfaceKey, flushSave]);

  const remove = useCallback(
    (id: string) => {
      setConversations((prev) => {
        const next = prev.filter((c) => c.id !== id);
        flushSave();
        save(surfaceKey, next);
        if (id === activeIdRef.current) {
          const nid = next[0]?.id ?? null;
          setActiveId(nid);
          activeIdRef.current = nid;
        }
        return next;
      });
    },
    [surfaceKey, flushSave],
  );

  return { conversations, activeId, activeMsgs, commit, select, create, remove };
}
