import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { Markdown } from '../../lib/markdown';
import { api, type ProjectRead } from '../../lib/api';
import { assistantTurnSse, type AssistantBlock } from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';

/* ============================================================
   Polaris 助手：全局抽屉。⌘J 开关。

   与现有六个对话入口并存——它们一行没改。助手能跨课题/库/论文调用平台工具，
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
  if (detail === 'PROJECT_REQUIRED') {
    return tr(
      '你有多个课题，先在上面选一个，助手才知道去哪儿查资料。',
      'You have several topics — pick one above so the assistant knows where to search.',
    );
  }
  if (detail === 'PROJECT_NOT_FOUND') {
    return tr('这个课题不存在，或者你已经不是它的成员了。', 'That topic does not exist, or you are no longer a member.');
  }
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

function BlockView({ block }: { block: AssistantBlock }) {
  if (block.kind === 'text') return <Markdown source={block.text} />;
  if (block.kind === 'thinking') {
    return (
      <details style={{ margin: '4px 0' }}>
        <summary style={{ fontSize: 11.5, color: 'var(--text-4)', cursor: 'pointer' }}>
          {tr('思考过程', 'Thinking')}
        </summary>
        <div style={{ fontSize: 12, color: 'var(--text-3)', whiteSpace: 'pre-wrap' }}>{block.text}</div>
      </details>
    );
  }
  if (block.kind === 'tool') return <ToolCard block={block} />;
  return null; // 未知块：画不出来就不画，绝不抛
}

export function AssistantPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [convId, setConvId] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [conversations, setConversations] = useState<
    { id: string; title: string; project_id: string | null }[]
  >([]);
  // 这轮检索的课题作用域。名下只有一个课题时后端会自己认，但选出来更诚实——
  // 用户看得见助手在哪儿查。多于一个且没选，后端 409，见 errorText。
  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  //: 上一轮因为没选课题而失败过——把选择器点亮，别让人对着一句错误发呆
  const [projectMissing, setProjectMissing] = useState(false);
  const abortRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [turns]);

  useEffect(() => () => abortRef.current?.(), []);

  // 面板打开时才拉课题列表：关着的时候没人看得见，不值得多一个请求。
  useEffect(() => {
    if (!open) return;
    let alive = true;
    void (async () => {
      try {
        const rows = await api.listProjects();
        if (!alive) return;
        setProjects(rows);
        // 只有一个课题就直接替他选上——这不算「替用户猜」，没有别的可能。
        setProjectId((cur) => cur ?? (rows.length === 1 ? (rows[0]?.id ?? null) : null));
      } catch {
        if (alive) setProjects([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, [open]);

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
        setProjectMissing(false);
        // 会话上存着的课题才是这场对话真正的作用域，跟着切过去
        const stored = conversations.find((c) => c.id === id)?.project_id ?? null;
        if (stored) setProjectId(stored);
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
    setProjectMissing(false);
  }, [stop]);

  const send = useCallback(async () => {
    const question = input.trim();
    if (!question || busy) return;
    setInput('');
    setBusy(true);
    setTurns((t) => [...t, { role: 'user', blocks: [{ kind: 'text', text: question }] }, { role: 'assistant', blocks: [] }]);

    let id = convId;
    try {
      if (!id) {
        id = (await api.createAssistantConversation(projectId ? { project_id: projectId } : {})).id;
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
        onBlocks: patch,
        onDone: () => setBusy(false),
        onError: (detail) => {
          if (detail === 'PROJECT_REQUIRED') setProjectMissing(true);
          patch((blocks) => [...blocks, { kind: 'text', text: `⚠️ ${errorText(detail)}` }]);
          setBusy(false);
        },
      },
      projectId,
    );
  }, [input, busy, convId, projectId]);

  if (!open) return null;

  return (
    <div
      style={{
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
      }}
    >
      <div className="row gap8" style={{ padding: '12px 16px', borderBottom: '0.5px solid var(--border-2)', alignItems: 'center' }}>
        <Icon name="chat" size={15} style={{ color: 'var(--accent)' }} />
        <strong style={{ fontSize: 14 }}>{tr('Polaris 助手', 'Polaris assistant')}</strong>
        <span style={{ flex: 1 }} />
        {busy && (
          <button className="btn btn-ghost sm" onClick={stop} style={{ height: 24, fontSize: 11 }}>
            <Icon name="pause" size={11} /> {tr('停止', 'Stop')}
          </button>
        )}
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
        <button className="icon-btn" onClick={onClose} title={tr('关闭（⌘J）', 'Close (⌘J)')}>
          <Icon name="x" size={14} />
        </button>
      </div>

      {/* —— 作用域：助手拿哪个课题的资料去查 ——
          一个课题都没有时不显示：那种情况下助手本来就不带工具，摆个空下拉只会误导。 */}
      {projects.length > 0 && (
        <div
          className="row gap8"
          style={{
            padding: '8px 16px',
            borderBottom: '0.5px solid var(--border-2)',
            alignItems: 'center',
            background: projectMissing && !projectId ? 'var(--danger-bg, var(--surface-2))' : 'var(--surface-2)',
          }}
        >
          <Icon name="layers" size={13} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
          <span style={{ fontSize: 11.5, color: 'var(--text-3)', flexShrink: 0 }}>
            {tr('查这个课题', 'Search in')}
          </span>
          <select
            className="input"
            value={projectId ?? ''}
            onChange={(e) => {
              setProjectId(e.target.value || null);
              setProjectMissing(false);
            }}
            style={{
              flex: 1,
              minWidth: 0,
              height: 26,
              fontSize: 12,
              borderColor: projectMissing && !projectId ? 'var(--danger)' : undefined,
            }}
          >
            <option value="">{tr('未选 —— 只闲聊，不检索', 'None — chat only, no search')}</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
      )}

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
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.7 }}>
            {tr(
              '助手可以调用平台的检索工具去查东西，而不是只凭上下文猜。试试问「最近有哪些关于 planning 的新论文」。',
              'The assistant calls the platform’s search tools instead of guessing from context. Try asking what is new on a topic.',
            )}
          </div>
        )}
        {turns.map((turn, i) => (
          <div key={i} style={{ marginBottom: 14 }}>
            {turn.role === 'user' ? (
              <div style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)', borderRadius: 9, padding: '8px 11px', fontSize: 13 }}>
                {turn.blocks.map((b, j) => (b.kind === 'text' ? <span key={j}>{b.text}</span> : null))}
              </div>
            ) : (
              <div style={{ fontSize: 13, lineHeight: 1.7 }}>
                {turn.blocks.map((b, j) => (
                  <BlockView key={j} block={b} />
                ))}
                {busy && i === turns.length - 1 && turn.blocks.length === 0 && (
                  <span style={{ color: 'var(--text-4)', fontSize: 12 }}>{tr('思考中…', 'Thinking…')}</span>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      <div style={{ padding: 12, borderTop: '0.5px solid var(--border-2)' }}>
        <textarea
          className="textarea"
          rows={2}
          value={input}
          placeholder={tr('问点什么…（Enter 发送）', 'Ask something… (Enter to send)')}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          style={{ width: '100%', fontSize: 13 }}
        />
      </div>
    </div>
  );
}
