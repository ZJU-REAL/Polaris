import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { Markdown } from '../../lib/markdown';
import { api } from '../../lib/api';
import { assistantTurnSse, type AssistantBlock, type PlanStep } from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';
import { CapabilitiesBar } from './CapabilitiesBar';
import { ToolImages } from './ToolImages';
import { pageContextFrom } from './buddyContext';
import { TurnStatus } from './TurnStatus';

/* ============================================================
   PolarisBuddy：全局抽屉。⌘J 开关，或点右下角的悬浮球。

   与现有六个对话入口并存——它们一行没改。Buddy 能跨课题/库/论文调用平台工具，
   界面上多出来的就是「工具卡片」：正在调什么、调完拿到了什么。

   渲染必须**防御式**：SSE 过来的是 JSON.parse 出来的 any，TypeScript 只在编译期
   帮你。未知事件忽略、未知块画成占位，绝不抛——这个项目没有前端测试工具，
   运行时炸了没人拦得住。
   ============================================================ */

interface Turn {
  role: 'user' | 'assistant';
  blocks: AssistantBlock[];
}

/** 服务端持久化的块 → 前端块。认不出的形状一律降级成文本，绝不抛。 */
function restoreBlocks(m: { text: string; blocks: unknown[] }): AssistantBlock[] {
  const out: AssistantBlock[] = [];
  for (const raw of Array.isArray(m.blocks) ? m.blocks : []) {
    if (!raw || typeof raw !== 'object') continue;
    const b = raw as Record<string, unknown>;
    if (b.kind === 'text' && typeof b.text === 'string') {
      out.push({ kind: 'text', text: b.text });
    } else if (b.kind === 'thinking' && typeof b.text === 'string') {
      out.push({ kind: 'thinking', text: b.text });
    } else if (b.kind === 'tool_use') {
      out.push({
        kind: 'tool',
        id: String(b.id ?? ''),
        name: String(b.name ?? '?'),
        state: 'ok',
      });
    }
    // tool_result 等其余块不单独渲染：它们是模型的输入，不是回答的一部分
  }
  if (out.length === 0 && m.text) out.push({ kind: 'text', text: m.text });
  return out;
}

/** 后端错误码 → 用户看得懂的一句话。认不出的原样透出，别把线索吃掉。 */
function errorText(detail: string): string {
  if (detail === 'CHAT_AGENT_DISABLED') {
    return tr('助手在这个部署上没开启，找管理员开一下。', 'The assistant is switched off on this deployment — ask an admin to enable it.');
  }
  if (detail === 'LLM_NOT_CONFIGURED') {
    return tr('还没配可用的模型，去设置里配一个。', 'No usable model is configured yet — set one up in settings.');
  }
  if (detail === 'QUOTA_EXCEEDED') {
    return tr('你的 AI 用量已经用完了。', 'You have used up your AI quota.');
  }
  return detail;
}

function ToolCard({ block }: { block: Extract<AssistantBlock, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false);
  const color =
    block.state === 'error' ? 'var(--danger)' : block.state === 'ok' ? 'var(--ok-tx)' : 'var(--text-4)';
  return (
    <div
      style={{
        border: '0.5px solid var(--border-2)',
        borderRadius: 8,
        background: 'var(--surface-2)',
        padding: '7px 10px',
        margin: '6px 0',
        fontSize: 12,
      }}
    >
      <div
        className="row gap8 hoverable"
        style={{ cursor: 'pointer', alignItems: 'center' }}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon
          name={block.state === 'running' ? 'refresh' : block.state === 'error' ? 'x' : 'check'}
          size={12}
          style={{
            color,
            animation: block.state === 'running' ? 'spin 1s linear infinite' : undefined,
          }}
        />
        <span className="mono" style={{ color: 'var(--text-2)' }}>{block.name}</span>
        <span style={{ color: 'var(--text-3)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {block.summary ?? tr('调用中…', 'running…')}
        </span>
        {block.durationMs !== undefined && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
            {block.durationMs}ms
          </span>
        )}
      </div>
      {block.images?.length ? <ToolImages images={block.images} /> : null}
      {open && block.preview && (
        <pre
          className="mono"
          style={{
            marginTop: 6,
            maxHeight: 220,
            overflow: 'auto',
            fontSize: 11,
            color: 'var(--text-3)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
          }}
        >
          {block.preview}
        </pre>
      )}
    </div>
  );
}

/** 任务计划：做到哪一步了。步骤状态是模型自己更新的，界面只是如实画出来。 */
function PlanCard({ steps }: { steps: PlanStep[] }) {
  const done = steps.filter((s) => s.status === 'done').length;
  return (
    <div
      style={{
        border: '0.5px solid var(--border-2)',
        borderRadius: 9,
        background: 'var(--surface-2)',
        padding: '9px 11px',
        margin: '8px 0',
      }}
    >
      <div
        className="row gap6"
        style={{ alignItems: 'center', marginBottom: 6, fontSize: 11.5, color: 'var(--text-3)' }}
      >
        <Icon name="layers" size={12} style={{ color: 'var(--accent)' }} />
        <span>{tr('计划', 'Plan')}</span>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
          {done}/{steps.length}
        </span>
      </div>
      {steps.map((step, i) => (
        <div
          key={`${i}-${step.title}`}
          className="row gap6"
          style={{
            alignItems: 'flex-start',
            fontSize: 12.5,
            lineHeight: 1.6,
            color: step.status === 'done' ? 'var(--text-4)' : 'var(--text-2)',
          }}
        >
          <Icon
            name={step.status === 'done' ? 'check' : step.status === 'running' ? 'refresh' : 'dot'}
            size={11}
            style={{
              marginTop: 4,
              flexShrink: 0,
              color:
                step.status === 'done'
                  ? 'var(--ok-tx)'
                  : step.status === 'running'
                    ? 'var(--accent)'
                    : 'var(--text-4)',
              animation: step.status === 'running' ? 'spin 1.1s linear infinite' : undefined,
            }}
          />
          <span
            style={{
              textDecoration: step.status === 'done' ? 'line-through' : undefined,
              fontWeight: step.status === 'running' ? 600 : 400,
            }}
          >
            {step.title}
          </span>
        </div>
      ))}
    </div>
  );
}

/** 思考块：跑的时候只露最后一行（瞟一眼就够），停了折起来。 */
function ThinkingView({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(false);
  const tail = text.trim().split('\n').filter(Boolean).slice(-1)[0] ?? '';
  return (
    <div style={{ margin: '4px 0' }}>
      <div
        className="row gap6 hoverable"
        style={{ cursor: 'pointer', alignItems: 'center', fontSize: 11.5, color: 'var(--text-4)' }}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon
          name="chevDown"
          size={11}
          style={{ transform: open ? undefined : 'rotate(-90deg)', transition: 'transform 0.12s' }}
        />
        <span>{tr('思考过程', 'Thinking')}</span>
        {!open && live && (
          <span
            style={{
              flex: 1,
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              opacity: 0.75,
            }}
          >
            {tail}
          </span>
        )}
      </div>
      {open && (
        <div
          style={{
            fontSize: 12,
            color: 'var(--text-3)',
            whiteSpace: 'pre-wrap',
            borderLeft: '2px solid var(--border-2)',
            paddingLeft: 9,
            marginTop: 4,
            lineHeight: 1.6,
          }}
        >
          {text}
        </div>
      )}
    </div>
  );
}

function BlockView({ block, live }: { block: AssistantBlock; live: boolean }) {
  if (block.kind === 'text') return <Markdown source={block.text} />;
  if (block.kind === 'plan') return <PlanCard steps={block.steps} />;
  if (block.kind === 'thinking') return <ThinkingView text={block.text} live={live} />;
  if (block.kind === 'tool') return <ToolCard block={block} />;
  return null; // 未知块：画不出来就不画，绝不抛
}

export function AssistantPanel({
  open,
  onClose,
  droppedPaperId,
  onDroppedPaperHandled,
  droppedText,
  onDroppedTextHandled,
  onBusyChange,
  variant = 'overlay',
}: {
  open: boolean;
  onClose: () => void;
  /** dock = 版面的一列（宽度与边框由 BuddyDock 负责）；overlay = 窄屏覆盖式抽屉 */
  variant?: 'dock' | 'overlay';
  /** 拖到悬浮球上的论文 id：进来就自动问一句「解读这篇」 */
  droppedPaperId?: string | null;
  onDroppedPaperHandled?: () => void;
  /** 拖到悬浮球上的一段文字：直接问这段 */
  droppedText?: string | null;
  onDroppedTextHandled?: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [convId, setConvId] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [conversations, setConversations] = useState<
    { id: string; title: string; project_id: string | null }[]
  >([]);
  const [greeting, setGreeting] = useState<string>('');
  const [stats, setStats] = useState<Record<string, number>>({});
  const [contextOn, setContextOn] = useState(true);
  const abortRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const location = useLocation();
  const pageContext = pageContextFrom(location.pathname);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [turns]);

  useEffect(() => () => abortRef.current?.(), []);

  useEffect(() => {
    onBusyChange?.(busy);
  }, [busy, onBusyChange]);

  // 开面板时取一次问候语。数字是 SQL 数出来的，**不过模型**——每次开面板过一次 LLM
  // 既慢又费钱，而且模型会把数字说错。取不到就不说，不要兜一句假的（0 和「没数出来」
  // 在界面上长得一样，对用户却是两回事）。
  useEffect(() => {
    if (!open || greeting) return;
    void api
      .getBuddyGreeting()
      .then((g) => {
        setGreeting(g.greeting);
        setStats(g.stats ?? {});
      })
      .catch(() => setGreeting(''));
  }, [open, greeting]);

  const stop = useCallback(() => {
    // 掐掉 SSE 连接。后端会把已生成的部分落库标成 interrupted，重开会话还能看到。
    abortRef.current?.();
    abortRef.current = null;
    setBusy(false);
  }, []);

  const openHistory = useCallback(async () => {
    setHistoryOpen((o) => !o);
    try {
      setConversations(await api.listAssistantConversations());
    } catch {
      setConversations([]);
    }
  }, []);

  const loadConversation = useCallback(
    async (id: string) => {
      // 服务端才是历史的权威源：本地状态直接整体替换
      try {
        const messages = await api.getAssistantMessages(id);
        setConvId(id);
        setHistoryOpen(false);
        // 会话上存着的课题才是这场对话真正的作用域，跟着切过去
        setTurns(
          messages
            .filter((m) => m.role === 'user' || m.role === 'assistant')
            .map((m) => ({
              role: m.role as 'user' | 'assistant',
              blocks: restoreBlocks(m),
            })),
        );
      } catch {
        /* 载入失败保持现状 */
      }
    },
    [conversations],
  );

  const newConversation = useCallback(() => {
    stop();
    setConvId(null);
    setTurns([]);
    setHistoryOpen(false);
  }, [stop]);

  const ask = useCallback(
    async (question: string) => {
    if (!question || busy) return;
    setBusy(true);
    setTurns((t) => [...t, { role: 'user', blocks: [{ kind: 'text', text: question }] }, { role: 'assistant', blocks: [] }]);

    let id = convId;
    try {
      if (!id) {
        id = (await api.createAssistantConversation({})).id;
        setConvId(id);
      }
    } catch (e) {
      // 这里以前把所有失败都说成「助手未启用」，把真正的原因吃掉了
      const detail = e instanceof Error ? e.message : String(e);
      setTurns((t) => {
        const next = [...t];
        next[next.length - 1] = { role: 'assistant', blocks: [{ kind: 'text', text: `⚠️ ${errorText(detail)}` }] };
        return next;
      });
      setBusy(false);
      return;
    }

    const patch = (fn: (blocks: AssistantBlock[]) => AssistantBlock[]) =>
      setTurns((t) => {
        const next = [...t];
        const last = next[next.length - 1];
        if (!last || last.role !== 'assistant') return t;
        next[next.length - 1] = { ...last, blocks: fn(last.blocks) };
        return next;
      });

    abortRef.current = assistantTurnSse(
      id,
      question,
      {
        // 页面上下文是**线索不是授权**：它只说用户在看什么，真要读内容还得调工具，
        // 那条路上的权限校验一点没少。用户可以关掉。
        page:
          contextOn && pageContext ? { kind: pageContext.kind, id: pageContext.id } : undefined,
        onBlocks: patch,
        onDone: () => setBusy(false),
        onError: (detail) => {
          patch((blocks) => [...blocks, { kind: 'text', text: `⚠️ ${errorText(detail)}` }]);
          setBusy(false);
        },
      },
    );
    },
    [busy, convId, contextOn, pageContext],
  );

  const send = useCallback(() => {
    const question = input.trim();
    if (!question) return;
    setInput('');
    void ask(question);
  }, [input, ask]);

  // 论文被拖到悬浮球上：直接问一句解读。id 写进问题里，Buddy 自己去 get_paper。
  useEffect(() => {
    if (!droppedPaperId || busy) return;
    onDroppedPaperHandled?.();
    void ask(
      tr(
        `帮我解读这篇论文（id: ${droppedPaperId}）：它解决什么问题、方法怎么工作、证据强不强。`,
        `Walk me through this paper (id: ${droppedPaperId}): the problem, how the method works, how strong the evidence is.`,
      ),
    );
  }, [droppedPaperId, busy, ask, onDroppedPaperHandled]);

  // 选中的文字被拖过来：把原文原样带上，让 Buddy 就着这段说
  useEffect(() => {
    if (!droppedText || busy) return;
    onDroppedTextHandled?.();
    void ask(
      tr(
        `解释一下这段话，必要时去平台里查证：\n\n「${droppedText}」`,
        `Explain this passage, checking the platform if needed:\n\n“${droppedText}”`,
      ),
    );
  }, [droppedText, busy, ask, onDroppedTextHandled]);

  if (!open) return null;

  // 快捷动作按真实计数给：没有实验就不该出现「实验怎么样了」
  const suggestions = [
    stats.experiments_running ? tr('我的实验现在怎么样了？', 'How are my experiments doing?') : '',
    stats.daily_today ? tr('帮我筛一遍今天的新论文', 'Triage today’s new papers for me') : '',
    stats.saved_recent
      ? tr('这周我收的论文有什么共同线索？', 'What connects the papers I saved this week?')
      : '',
    pageContext?.kind === 'paper' ? tr('解读我正在看的这篇', 'Walk me through this paper') : '',
  ].filter(Boolean);

  const lastTurn = turns[turns.length - 1];
  const liveBlocks = busy && lastTurn?.role === 'assistant' ? lastTurn.blocks : [];

  // 这场对话里真的被加载过的技能。判据是 skill_load 的调用摘要——**不是**目录里
  // 有哪些技能：目录只说明「它可能会用」，加载过才是「它真的用了」。
  const loadedSkills = new Set<string>();
  for (const turn of turns) {
    for (const block of turn.blocks) {
      if (block.kind === 'tool' && block.name === 'skill_load' && block.state === 'ok') {
        const slug = (block.summary ?? '').match(/[a-z0-9][a-z0-9-]{2,}/i)?.[0];
        if (slug) loadedSkills.add(slug);
      }
    }
  }

  // dock 形态下，外框（宽度/边框/拖拽把手）归 BuddyDock 管，这里只铺内容；
  // overlay 形态自己是那个浮层。两种形态共用下面同一套正文，不分叉。
  const frame: React.CSSProperties =
    variant === 'dock'
      ? { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }
      : {
          position: 'fixed',
          top: 0,
          right: 0,
          bottom: 0,
          width: 'min(520px, 100vw)',
          background: 'var(--surface)',
          borderLeft: '0.5px solid var(--border-2)',
          display: 'flex',
          flexDirection: 'column',
          zIndex: 60,
          boxShadow: '-8px 0 24px rgba(0,0,0,0.06)',
        };

  return (
    <div style={frame}>
      <div className="row gap8" style={{ padding: '12px 16px', borderBottom: '0.5px solid var(--border-2)', alignItems: 'center' }}>
        <Icon name="sparkle" size={15} style={{ color: 'var(--accent)' }} />
        <strong style={{ fontSize: 14 }}>PolarisBuddy</strong>
        <span style={{ flex: 1 }} />
        <button
          className="icon-btn"
          onClick={() => void openHistory()}
          title={tr('历史会话', 'Conversations')}
        >
          <Icon name="clock" size={14} />
        </button>
        <button className="icon-btn" onClick={newConversation} title={tr('新会话', 'New')}>
          <Icon name="plus" size={14} />
        </button>
        <button
          className="icon-btn"
          onClick={onClose}
          title={
            variant === 'dock'
              ? tr('收起（⌘J）', 'Collapse (⌘J)')
              : tr('关闭（⌘J）', 'Close (⌘J)')
          }
        >
          <Icon name={variant === 'dock' ? 'chevron' : 'x'} size={14} />
        </button>
      </div>

      <CapabilitiesBar loadedSkills={loadedSkills} />

      {historyOpen && (
        <div
          style={{
            borderBottom: '0.5px solid var(--border-2)',
            maxHeight: 220,
            overflowY: 'auto',
            padding: '6px 8px',
          }}
        >
          {conversations.length === 0 && (
            <div style={{ fontSize: 12, color: 'var(--text-4)', padding: 8 }}>
              {tr('还没有历史会话', 'No conversations yet')}
            </div>
          )}
          {conversations.map((c) => (
            <div
              key={c.id}
              className="hoverable"
              onClick={() => void loadConversation(c.id)}
              style={{
                padding: '7px 9px',
                borderRadius: 7,
                fontSize: 12.5,
                cursor: 'pointer',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                background: c.id === convId ? 'var(--accent-soft)' : undefined,
              }}
            >
              {c.title || tr('（未命名）', '(untitled)')}
            </div>
          ))}
        </div>
      )}

      <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '14px 16px' }}>
        {turns.length === 0 && (
          <div style={{ fontSize: 12.5, lineHeight: 1.7 }}>
            {greeting && (
              <div
                style={{
                  background: 'var(--accent-soft)',
                  color: 'var(--accent-text)',
                  borderRadius: 10,
                  padding: '10px 12px',
                  marginBottom: 10,
                  fontSize: 13,
                }}
              >
                {greeting}
              </div>
            )}
            <div style={{ color: 'var(--text-3)' }}>
              {tr(
                '我会调用平台的检索工具去查，而不是凭上下文猜。把论文拖到右下角的球上，我就替你读它。',
                'I call the platform’s search tools instead of guessing. Drop a paper on the bubble and I’ll read it for you.',
              )}
            </div>
            {/* 快捷动作只在对应数据真的存在时出现：没有实验就不该问「实验怎么样了」 */}
            <div className="row gap6 wrap" style={{ marginTop: 10 }}>
              {suggestions.map((text) => (
                <span
                  key={text}
                  className="chip"
                  style={{ cursor: 'pointer' }}
                  onClick={() => void ask(text)}
                >
                  {text}
                </span>
              ))}
            </div>
          </div>
        )}
        {turns.map((turn, i) => {
          const isLast = i === turns.length - 1;
          return (
            <div key={i} style={{ marginBottom: 14 }}>
              {turn.role === 'user' ? (
                <div style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)', borderRadius: 9, padding: '8px 11px', fontSize: 13 }}>
                  {turn.blocks.map((b, j) => (b.kind === 'text' ? <span key={j}>{b.text}</span> : null))}
                </div>
              ) : (
                <div style={{ fontSize: 13, lineHeight: 1.7 }}>
                  {turn.blocks.map((b, j) => (
                    <BlockView
                      key={j}
                      block={b}
                      live={busy && isLast && j === turn.blocks.length - 1}
                    />
                  ))}
                  {isLast && <TurnStatus blocks={liveBlocks} busy={busy} onStop={stop} />}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div style={{ padding: 12, borderTop: '0.5px solid var(--border-2)' }}>
        {pageContext && (
          <div
            className="row gap6"
            style={{ alignItems: 'center', marginBottom: 6, fontSize: 11, color: 'var(--text-4)' }}
            title={tr(
              '我知道你在看什么。关掉的话这轮就不带页面信息了。',
              'I can see what you are looking at. Turn it off to leave the page out of this turn.',
            )}
          >
            <input
              type="checkbox"
              checked={contextOn}
              onChange={(e) => setContextOn(e.target.checked)}
              style={{ width: 12, height: 12, margin: 0, accentColor: 'var(--accent)', cursor: 'pointer' }}
            />
            <span>{pageContext.label}</span>
          </div>
        )}
        <textarea
          className="textarea"
          rows={2}
          value={input}
          placeholder={tr('问点什么…（Enter 发送）', 'Ask something… (Enter to send)')}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          style={{ width: '100%', fontSize: 13 }}
        />
      </div>
    </div>
  );
}
