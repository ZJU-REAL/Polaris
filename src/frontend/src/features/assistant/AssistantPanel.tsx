import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { Markdown } from '../../lib/markdown';
import { api } from '../../lib/api';
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
  const abortRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [turns]);

  useEffect(() => () => abortRef.current?.(), []);

  const send = useCallback(async () => {
    const question = input.trim();
    if (!question || busy) return;
    setInput('');
    setBusy(true);
    setTurns((t) => [...t, { role: 'user', blocks: [{ kind: 'text', text: question }] }, { role: 'assistant', blocks: [] }]);

    let id = convId;
    try {
      if (!id) {
        id = (await api.createAssistantConversation()).id;
        setConvId(id);
      }
    } catch {
      setTurns((t) => {
        const next = [...t];
        next[next.length - 1] = { role: 'assistant', blocks: [{ kind: 'text', text: tr('助手未启用。', 'The assistant is not enabled.') }] };
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

    abortRef.current = assistantTurnSse(id, question, {
      onBlocks: patch,
      onDone: () => setBusy(false),
      onError: (detail) => {
        patch((blocks) => [...blocks, { kind: 'text', text: `⚠️ ${detail}` }]);
        setBusy(false);
      },
    });
  }, [input, busy, convId]);

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
        <button className="icon-btn" onClick={onClose} title={tr('关闭（⌘J）', 'Close (⌘J)')}>
          <Icon name="x" size={14} />
        </button>
      </div>

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
