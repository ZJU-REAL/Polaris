import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon, type IconName } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { api, skillKindLabel, type ChatTurn } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useChatHistory } from './useChatHistory';
import { Composer } from './Composer';
import { CONTEXT_KIND_META, type ChatMsg, type ContextKind, type ContextRef, type MentionTarget } from './types';

/* ============================================================
   共享对话框：左侧历史抽屉 + 顶部技能条 + 消息区 + 输入区。
   文献对话 / AI 伴读 都用它，差异通过 config 注入。
   ============================================================ */

export interface StreamController {
  onDelta: (text: string) => void;
  onSources?: (dataStr: string) => void;
  onDone: () => void;
  onError: (detail: string) => void;
}

export interface ChatSurfaceConfig {
  /** localStorage 命名空间，如 `library:${pid}` / `reading:${paperId}` */
  surfaceKey: string;
  pid: string;
  title: string;
  contextKinds: ContextKind[];
  hint: ReactNode;
  headerAction?: ReactNode;
  emptyIcon: IconName;
  emptyTitle: string;
  emptyDesc: string;
  suggestions?: { zh: string; en: string }[];
  placeholder: string;
  /** 窄面板（如阅读页侧栏）默认收起历史抽屉 */
  defaultDrawerOpen?: boolean;
  attachesPaperLink?: boolean;
  /** 分享出去时附带的论文阅读链接（AI 伴读传） */
  shareLink?: string | null;
  /** 渲染 assistant 正文（库对话注入引用角标/双链渲染器） */
  renderAssistant: (msg: ChatMsg) => ReactNode;
  /** assistant 气泡内附加区（库对话来源清单） */
  assistantExtras?: (msg: ChatMsg) => ReactNode;
  /** assistant 气泡底部动作（伴读存为笔记） */
  messageActions?: (msg: ChatMsg, question: string) => ReactNode;
  /** SSE 发送 */
  stream: (
    args: { question: string; history: ChatTurn[]; context: ContextRef[] },
    ctrl: StreamController,
  ) => () => void;
}

const MAX_HISTORY_TURNS = 10;

function recommendPrompt(context: ContextRef[], shareLink?: string | null): string {
  const items = context.length
    ? context.map((c) => `- ${c.label}`).join('\n')
    : '';
  return [
    '请用中文写一段简洁、真诚的推荐语（3-4 句），推荐给同事，说明它值得一读的理由。',
    items ? `推荐对象：\n${items}` : '推荐对象：当前讨论的内容。',
    shareLink ? '结尾自然地提到可以点开阅读链接查看原文。' : '',
    '直接输出推荐语正文，不要加“推荐语：”之类的前缀。',
  ].filter(Boolean).join('\n\n');
}

export function ChatSurface(cfg: ChatSurfaceConfig) {
  const history = useChatHistory(cfg.surfaceKey);
  const { activeMsgs, commit } = history;
  const [streaming, setStreaming] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(cfg.defaultDrawerOpen ?? true);
  const stopRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // 是否「贴着底部」：用户往上滚离底部后暂停自动跟随，滚回底部再恢复
  const stickBottomRef = useRef(true);
  // 提交后待发的分享目标（回答完成时触发桩投递提示）
  const pendingShareRef = useRef<MentionTarget | null>(null);

  const skillsQ = useQuery({
    queryKey: ['project-skills', cfg.pid],
    queryFn: () => api.listProjectSkills(cfg.pid),
    enabled: !!cfg.pid,
    retry: false,
    staleTime: 60_000,
  });
  const enabledSkills = (skillsQ.data ?? []).filter((s) => s.enabled && s.skill);

  // 用 ref 保持最新 msgs 供 SSE 回调闭包读取（避免 stale）
  const activeMsgsRef = useRef<ChatMsg[]>(activeMsgs);
  activeMsgsRef.current = activeMsgs;

  useEffect(() => () => stopRef.current?.(), []);
  // 只有用户仍贴在底部时才自动跟随到底；往上滚阅读时不打断
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [activeMsgs]);

  const onMsgsScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };

  const finishStream = (failed: boolean, fallbackText?: string) => {
    setStreaming(false);
    stopRef.current = null;
    commit(
      withLastAssistant(activeMsgsRef.current, (last) => ({
        ...last,
        done: true,
        failed: failed || undefined,
        content: last.content || fallbackText || '',
      })),
    );
    // 桩投递：回答完成后提示「已排队，后端待接」
    const share = pendingShareRef.current;
    pendingShareRef.current = null;
    if (share && !failed) {
      const link = cfg.shareLink ? tr('，已附论文链接', ' with paper link') : '';
      toast(
        tr(`已排队分享给 ${share.label}${link}（后端待接）`, `Queued to share with ${share.label}${link} (backend pending)`),
        'ok',
      );
    }
  };

  const send = (payload: { text: string; context: ContextRef[]; shareTo: MentionTarget | null }) => {
    if (streaming) return;
    const recommend = payload.shareTo !== null && payload.text.trim().length === 0;
    let question = recommend ? recommendPrompt(payload.context, cfg.shareLink) : payload.text.trim();
    if (!question) return;
    // / 选中的上下文并入发给模型的问题（展示的 user 正文保持干净）
    if (!recommend && payload.context.length) {
      const ctxLine = payload.context
        .map((c) => `【${tr(CONTEXT_KIND_META[c.kind].zh, CONTEXT_KIND_META[c.kind].en)}】${c.label}`)
        .join('，');
      question = `${question}\n\n（请重点结合以下上下文：${ctxLine}）`;
    }

    const history10: ChatTurn[] = activeMsgsRef.current
      .filter((m) => m.content && !m.failed)
      .slice(-MAX_HISTORY_TURNS * 2)
      .map((m) => ({ role: m.role, content: m.content }));

    const userMsg: ChatMsg = {
      role: 'user',
      content: payload.text.trim(),
      context: payload.context.length ? payload.context : undefined,
      sharedTo: payload.shareTo,
    };
    const base = [...activeMsgsRef.current, userMsg, { role: 'assistant' as const, content: '' }];
    activeMsgsRef.current = base;
    stickBottomRef.current = true; // 新一轮提问：跟随到底，把这轮滚进视野
    commit(base);
    setStreaming(true);
    pendingShareRef.current = payload.shareTo;

    stopRef.current = cfg.stream(
      { question, history: history10, context: payload.context },
      {
        onDelta: (text) => {
          if (!text) return;
          const next = withLastAssistant(activeMsgsRef.current, (last) =>
            last.done ? last : { ...last, content: last.content + text },
          );
          activeMsgsRef.current = next;
          commit(next);
        },
        onSources: (dataStr) => {
          let items: ChatMsg['sources'] = [];
          try {
            items = (JSON.parse(dataStr) as { items?: ChatMsg['sources'] }).items ?? [];
          } catch {
            return;
          }
          const next = withLastAssistant(activeMsgsRef.current, (last) =>
            last.done ? last : { ...last, sources: items },
          );
          activeMsgsRef.current = next;
          commit(next);
        },
        onDone: () => finishStream(false),
        onError: (detail) => {
          toast(`${tr('对话出错：', 'Chat error: ')}${detail}`, 'error');
          finishStream(true, tr('（回答中断了，请重试）', '(Answer interrupted — please retry)'));
        },
      },
    );
  };

  const stop = () => {
    stopRef.current?.();
    finishStream(false);
  };

  const questionFor = (idx: number): string => {
    for (let i = idx - 1; i >= 0; i--) {
      const m = activeMsgs[i];
      if (m && m.role === 'user') return m.content;
    }
    return '';
  };

  return (
    <div className={'chat-shell' + (drawerOpen ? '' : ' drawer-collapsed')}>
      {/* —— 历史抽屉 —— */}
      <div className="chat-history">
        <div className="chat-history-head">
          <span className="mono">{tr('历史对话', 'History')}</span>
          <button className="chat-new-btn" title={tr('新建对话', 'New chat')} onClick={history.create}>
            <Icon name="plus" size={13} />
          </button>
        </div>
        <div className="chat-history-list scroll">
          {history.conversations.length === 0 ? (
            <div className="chat-history-empty">{tr('还没有历史对话', 'No conversations yet')}</div>
          ) : (
            history.conversations.map((c) => (
              <div
                key={c.id}
                className={'chat-history-item' + (c.id === history.activeId ? ' on' : '')}
                onClick={() => history.select(c.id)}
              >
                <Icon name="chat" size={12} />
                <span className="chat-history-title">{c.title || tr('新对话', 'New chat')}</span>
                <button
                  className="chat-history-x"
                  title={tr('删除', 'Delete')}
                  onClick={(e) => {
                    e.stopPropagation();
                    history.remove(c.id);
                  }}
                >
                  <Icon name="trash" size={11} />
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* —— 主对话列 —— */}
      <div className="chat-main">
        <div className="chat-topbar">
          <button
            className="chat-tool"
            title={drawerOpen ? tr('收起历史', 'Hide history') : tr('展开历史', 'Show history')}
            onClick={() => setDrawerOpen((o) => !o)}
          >
            <Icon name="sidebar" size={15} />
          </button>
          {enabledSkills.length > 0 ? (
            <div className="chat-skillbar scroll">
              <span className="chat-skill-label mono">{tr('已启用技能', 'Active skills')}</span>
              {enabledSkills.map((s) => (
                <span
                  key={s.id}
                  className="chat-skill"
                  title={`${s.skill?.name ?? ''} · ${skillKindLabel(s.skill!.kind)}`}
                >
                  <Icon name="sparkle" size={10} />
                  {s.skill?.name}
                </span>
              ))}
            </div>
          ) : (
            <div className="chat-skillbar">
              <span className="chat-skill-label mono" style={{ opacity: 0.7 }}>
                {tr('未启用技能', 'No active skills')}
              </span>
            </div>
          )}
          {cfg.headerAction}
        </div>

        <div className="chat-hint">{cfg.hint}</div>

        <div ref={scrollRef} className="chat-msgs scroll" onScroll={onMsgsScroll}>
          {activeMsgs.length === 0 ? (
            <div className="chat-empty-wrap">
              <EmptyState compact icon={cfg.emptyIcon} title={cfg.emptyTitle} desc={cfg.emptyDesc} />
              {cfg.suggestions && cfg.suggestions.length > 0 && (
                <div className="col gap8" style={{ marginTop: 18 }}>
                  {cfg.suggestions.map((s) => (
                    <button
                      key={s.zh}
                      className="chat-suggestion"
                      onClick={() => send({ text: tr(s.zh, s.en), context: [], shareTo: null })}
                    >
                      {tr(s.zh, s.en)}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            activeMsgs.map((m, i) => {
              if (m.role === 'user') {
                const recoLabel = !m.content && m.sharedTo;
                return (
                  <div key={i} className="chat-row user">
                    <div className="chat-bubble-user">
                      {(m.context?.length || m.sharedTo) && (
                        <div className="chat-bubble-meta">
                          {m.context?.map((c) => (
                            <span key={`${c.kind}:${c.id}`} className="chat-meta-chip">
                              <Icon name="link" size={9} />
                              {c.label}
                            </span>
                          ))}
                          {m.sharedTo && (
                            <span className="chat-meta-chip share">
                              @{m.sharedTo.label}
                            </span>
                          )}
                        </div>
                      )}
                      {recoLabel ? (
                        <span className="chat-reco-label">
                          {tr(`让 AI 写推荐并分享给 ${m.sharedTo!.label}`, `Ask AI to recommend & share with ${m.sharedTo!.label}`)}
                        </span>
                      ) : (
                        m.content
                      )}
                    </div>
                  </div>
                );
              }
              const thinking = !m.done && !m.content;
              return (
                <div key={i} className="chat-row ai">
                  <div className="chat-bubble-ai">
                    {thinking ? (
                      <span className="row gap6 muted" style={{ fontSize: 12 }}>
                        <Icon name="refresh" size={12} style={{ animation: 'spin 1s linear infinite' }} />
                        {tr('正在思考…', 'Thinking…')}
                      </span>
                    ) : (
                      cfg.renderAssistant(m)
                    )}
                    {cfg.assistantExtras?.(m)}
                    {m.done && !m.failed && m.content && cfg.messageActions?.(m, questionFor(i))}
                  </div>
                </div>
              );
            })
          )}
        </div>

        <Composer
          pid={cfg.pid}
          streaming={streaming}
          contextKinds={cfg.contextKinds}
          attachesPaperLink={cfg.attachesPaperLink}
          placeholder={cfg.placeholder}
          onSend={send}
          onStop={stop}
        />
      </div>
    </div>
  );
}

/** 把 msgs 里最后一条 assistant 用 fn 更新，返回新数组 */
function withLastAssistant(msgs: ChatMsg[], fn: (last: ChatMsg) => ChatMsg): ChatMsg[] {
  const next = [...msgs];
  const last = next[next.length - 1];
  if (last && last.role === 'assistant') next[next.length - 1] = fn(last);
  return next;
}
