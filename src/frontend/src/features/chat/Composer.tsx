import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon, type IconName } from '../../components/ui/Icon';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useProject } from '../../app/project';
import {
  CONTEXT_KIND_META,
  type ContextKind,
  type ContextRef,
  type MentionTarget,
} from './types';

/* ============================================================
   对话输入区：文本框 + 上下文 chips + 分享目标 chip
   + 「/」上下文选择器 + 「@」分享目标选择器（键盘可导航）。
   ============================================================ */

interface ComposerProps {
  pid: string;
  streaming: boolean;
  /** / 选择器提供哪些实体类型 */
  contextKinds: ContextKind[];
  /** 分享出去会附带论文阅读链接（AI 伴读场景），仅用于提示 */
  attachesPaperLink?: boolean;
  placeholder: string;
  onSend: (payload: { text: string; context: ContextRef[]; shareTo: MentionTarget | null }) => void;
  onStop: () => void;
}

const BOTS: MentionTarget[] = [
  { kind: 'dingtalk', id: 'bot:dingtalk', label: '钉钉机器人', sub: 'DingTalk 群' },
  { kind: 'feishu', id: 'bot:feishu', label: '飞书机器人', sub: 'Feishu 群' },
];

const MENTION_ICON: Record<MentionTarget['kind'], IconName> = {
  user: 'users',
  dingtalk: 'chat',
  feishu: 'chat',
};

// 末尾触发 token：空白或行首后的 / 或 @，后接非空白
const TRIGGER_RE = /(^|\s)([/@])(\S*)$/;

export function Composer({
  pid,
  streaming,
  contextKinds,
  attachesPaperLink,
  placeholder,
  onSend,
  onStop,
}: ComposerProps) {
  const { currentProject } = useProject();
  const [text, setText] = useState('');
  const [context, setContext] = useState<ContextRef[]>([]);
  const [shareTo, setShareTo] = useState<MentionTarget | null>(null);
  const [menu, setMenu] = useState<{ type: 'slash' | 'mention'; query: string } | null>(null);
  const [hi, setHi] = useState(0);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // —— / 选择器候选：按需拉取真实列表 ——
  const slashOpen = menu?.type === 'slash';
  const papersQ = useQuery({
    queryKey: ['chat-ctx-papers', pid],
    queryFn: () => api.listPapers(pid, { size: 60 }),
    enabled: slashOpen && contextKinds.includes('paper'),
    staleTime: 60_000,
  });
  const ideasQ = useQuery({
    queryKey: ['chat-ctx-ideas', pid],
    queryFn: () => api.listIdeas(pid),
    enabled: slashOpen && contextKinds.includes('idea'),
    staleTime: 60_000,
  });
  const expsQ = useQuery({
    queryKey: ['chat-ctx-exps', pid],
    queryFn: () => api.listExperiments(pid),
    enabled: slashOpen && contextKinds.includes('experiment'),
    staleTime: 60_000,
  });
  const conceptsQ = useQuery({
    queryKey: ['chat-ctx-concepts', pid],
    queryFn: () => api.listConcepts(pid),
    enabled: slashOpen && contextKinds.includes('concept'),
    staleTime: 60_000,
  });

  const chosen = useMemo(() => new Set(context.map((c) => `${c.kind}:${c.id}`)), [context]);

  const slashItems = useMemo<ContextRef[]>(() => {
    if (!slashOpen) return [];
    const out: ContextRef[] = [];
    if (contextKinds.includes('paper')) {
      for (const p of papersQ.data?.items ?? []) out.push({ kind: 'paper', id: p.id, label: p.title });
    }
    if (contextKinds.includes('idea')) {
      for (const i of ideasQ.data ?? []) out.push({ kind: 'idea', id: i.id, label: i.title });
    }
    if (contextKinds.includes('experiment')) {
      for (const e of expsQ.data ?? []) out.push({ kind: 'experiment', id: e.id, label: e.idea_title });
    }
    if (contextKinds.includes('concept')) {
      for (const c of conceptsQ.data ?? []) out.push({ kind: 'concept', id: c.id, label: c.name });
    }
    const q = (menu?.query ?? '').toLowerCase();
    return out
      .filter((r) => !chosen.has(`${r.kind}:${r.id}`))
      .filter((r) => !q || r.label.toLowerCase().includes(q))
      .slice(0, 40);
  }, [slashOpen, contextKinds, papersQ.data, ideasQ.data, expsQ.data, conceptsQ.data, menu?.query, chosen]);

  const mentionItems = useMemo<MentionTarget[]>(() => {
    if (menu?.type !== 'mention') return [];
    const users: MentionTarget[] = (currentProject?.members ?? [])
      .filter((m) => m.user_id)
      .map((m) => ({
        kind: 'user' as const,
        id: m.user_id!,
        label: m.display_name || m.email || tr('成员', 'Member'),
        sub: m.email ?? undefined,
      }));
    const all = [...users, ...BOTS];
    const q = menu.query.toLowerCase();
    return all.filter((t) => !q || t.label.toLowerCase().includes(q) || (t.sub ?? '').toLowerCase().includes(q));
  }, [menu, currentProject?.members]);

  const menuLen = slashOpen ? slashItems.length : mentionItems.length;
  useEffect(() => {
    setHi(0);
  }, [menu?.type, menu?.query]);

  function onChange(v: string) {
    setText(v);
    const m = TRIGGER_RE.exec(v);
    if (m) {
      setMenu({ type: m[2] === '/' ? 'slash' : 'mention', query: m[3] ?? '' });
    } else {
      setMenu(null);
    }
  }

  // 选中后把末尾触发 token 从文本里抹掉
  function stripTrigger() {
    setText((v) => v.replace(TRIGGER_RE, (_all, pre) => pre));
    setMenu(null);
    inputRef.current?.focus();
  }

  function pickContext(ref: ContextRef) {
    setContext((c) => (c.some((x) => x.kind === ref.kind && x.id === ref.id) ? c : [...c, ref]));
    stripTrigger();
  }
  function pickMention(t: MentionTarget) {
    setShareTo(t);
    stripTrigger();
  }

  function submit() {
    const t = text.trim();
    // @单独（无正文）= 让 AI 写推荐；有正文 = 对话后转发。两者都需有分享目标或正文之一。
    if (streaming) return;
    if (!t && !shareTo) return;
    onSend({ text: t, context, shareTo });
    setText('');
    setContext([]);
    setShareTo(null);
    setMenu(null);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (menu && menuLen > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setHi((h) => (h + 1) % menuLen);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setHi((h) => (h - 1 + menuLen) % menuLen);
        return;
      }
      if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
        e.preventDefault();
        if (slashOpen) {
          const it = slashItems[hi];
          if (it) pickContext(it);
        } else {
          const it = mentionItems[hi];
          if (it) pickMention(it);
        }
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setMenu(null);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  const canSend = !streaming && (text.trim().length > 0 || shareTo !== null);
  const recommendMode = shareTo !== null && text.trim().length === 0;

  return (
    <div className="chat-composer">
      {menu && menuLen > 0 && (
        <div className="chat-pop">
          <div className="chat-pop-head mono">
            {slashOpen
              ? tr('放入上下文 · 论文 / 实验 / 想法 / 概念', 'Add context · papers / experiments / ideas / concepts')
              : tr('分享给 · 成员或机器人', 'Share to · members or bots')}
          </div>
          <div className="chat-pop-list scroll">
            {slashOpen
              ? slashItems.map((it, i) => (
                  <button
                    key={`${it.kind}:${it.id}`}
                    className={'chat-pop-item' + (i === hi ? ' on' : '')}
                    onMouseEnter={() => setHi(i)}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      pickContext(it);
                    }}
                  >
                    <Icon name={CONTEXT_KIND_META[it.kind].icon as IconName} size={13} />
                    <span className="chat-pop-label">{it.label}</span>
                    <span className="chat-pop-kind mono">{tr(CONTEXT_KIND_META[it.kind].zh, CONTEXT_KIND_META[it.kind].en)}</span>
                  </button>
                ))
              : mentionItems.map((it, i) => (
                  <button
                    key={it.id}
                    className={'chat-pop-item' + (i === hi ? ' on' : '')}
                    onMouseEnter={() => setHi(i)}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      pickMention(it);
                    }}
                  >
                    <Icon name={MENTION_ICON[it.kind]} size={13} />
                    <span className="chat-pop-label">{it.label}</span>
                    {it.sub && <span className="chat-pop-kind mono">{it.sub}</span>}
                  </button>
                ))}
          </div>
        </div>
      )}

      {/* 上下文 chips + 分享目标 chip */}
      {(context.length > 0 || shareTo) && (
        <div className="chat-chips">
          {context.map((c) => (
            <span key={`${c.kind}:${c.id}`} className="chat-chip">
              <Icon name={CONTEXT_KIND_META[c.kind].icon as IconName} size={11} />
              <span className="chat-chip-label">{c.label}</span>
              <button
                className="chat-chip-x"
                title={tr('移除', 'Remove')}
                onClick={() => setContext((cs) => cs.filter((x) => !(x.kind === c.kind && x.id === c.id)))}
              >
                <Icon name="x" size={10} />
              </button>
            </span>
          ))}
          {shareTo && (
            <span className="chat-chip share">
              <Icon name={MENTION_ICON[shareTo.kind]} size={11} />
              <span className="chat-chip-label">{tr('分享给 ', 'To ')}{shareTo.label}</span>
              <button className="chat-chip-x" title={tr('取消分享', 'Cancel share')} onClick={() => setShareTo(null)}>
                <Icon name="x" size={10} />
              </button>
            </span>
          )}
        </div>
      )}

      {recommendMode && (
        <div className="chat-hint-line">
          <Icon name="sparkle" size={11} />
          {tr('直接发送 = 让 AI 写一段推荐语转给对方', 'Send as-is = AI writes a recommendation to forward')}
          {attachesPaperLink && tr('（附本篇阅读链接）', ' (with this paper’s read link)')}
        </div>
      )}

      <div className="chat-input-row">
        <button
          className="chat-tool"
          title={tr('放入上下文', 'Add context')}
          onMouseDown={(e) => {
            e.preventDefault();
            onChange((text ? text + ' ' : '') + '/');
            inputRef.current?.focus();
          }}
        >
          <Icon name="plus" size={15} />
        </button>
        <button
          className="chat-tool"
          title={tr('分享 / 召唤', 'Share / mention')}
          onMouseDown={(e) => {
            e.preventDefault();
            onChange((text ? text + ' ' : '') + '@');
            inputRef.current?.focus();
          }}
        >
          <span style={{ fontWeight: 700, fontSize: 15 }}>@</span>
        </button>
        <textarea
          ref={inputRef}
          className="chat-textarea"
          rows={1}
          placeholder={placeholder}
          value={text}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
        />
        {streaming ? (
          <button className="btn btn-ghost sm" style={{ height: 34 }} onClick={onStop}>
            <Icon name="pause" size={13} />
            {tr('停止', 'Stop')}
          </button>
        ) : (
          <button className="btn btn-primary sm" style={{ height: 34 }} disabled={!canSend} onClick={submit}>
            <Icon name={recommendMode ? 'sparkle' : 'arrow'} size={13} />
            {recommendMode ? tr('推荐', 'Recommend') : tr('发送', 'Send')}
          </button>
        )}
      </div>
    </div>
  );
}
