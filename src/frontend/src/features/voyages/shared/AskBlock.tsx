import { useState } from 'react';
import { Icon } from '../../../components/ui/Icon';
import { tr, useLang } from '../../../lib/i18n';
import type { VoyageMessageRead } from '../../../lib/api';
import { hhmmss } from './terminal';

/* ============================================================
   终端混排流里的对话块（深色终端配色，走 --terminal-* token）：
   - AskBlock：AI 提问（等回答时黄框高亮 + 候选项；已回答/已作废转灰）
   - UserNoteBlock：用户消息（建议 / 回答），右对齐
   - InfoNoteLine：AI 播报（「已采纳你的建议」等）
   ============================================================ */

export function askOptionLabel(opt: { id: string; zh?: string; en?: string }, lang: string): string {
  return (lang === 'en' ? opt.en : opt.zh) || opt.zh || opt.en || opt.id;
}

/** ask 的诊断上下文 → 可读文本（防御式：结构随 ask_kind 而变）。 */
function contextText(context: Record<string, unknown> | null | undefined): string | null {
  if (!context || typeof context !== 'object') return null;
  const parts: string[] = [];
  for (const [key, value] of Object.entries(context)) {
    if (value === null || value === undefined || value === '') continue;
    const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    parts.push(`${key}: ${text}`);
  }
  return parts.length > 0 ? parts.join('\n') : null;
}

export function AskBlock({ message }: { message: VoyageMessageRead }) {
  const [open, setOpen] = useState(false);
  const lang = useLang();
  const pendingAnswer = message.status === 'open';
  const superseded = message.status === 'superseded';
  const options = message.payload?.options ?? [];
  const ctx = contextText(message.payload?.context);
  const borderColor = pendingAnswer ? 'var(--terminal-warn)' : 'var(--terminal-border)';
  return (
    <div
      style={{
        margin: '8px 0',
        padding: '9px 11px',
        background: 'var(--terminal-bg-2)',
        border: `1px solid ${borderColor}`,
        borderRadius: 8,
        opacity: superseded ? 0.55 : 1,
      }}
    >
      <div className="row" style={{ gap: 7 }}>
        {pendingAnswer ? (
          <span className="dot pulse" style={{ background: 'var(--terminal-warn)', flexShrink: 0 }} />
        ) : (
          <Icon name="check" size={12} style={{ color: 'var(--terminal-dim)', flexShrink: 0 }} />
        )}
        <span style={{ color: 'var(--terminal-warn)', fontWeight: 700 }}>
          {tr('AI 提问', 'AI question')}
        </span>
        {superseded && (
          <span style={{ color: 'var(--terminal-dim)' }}>· {tr('已作废', 'superseded')}</span>
        )}
        {!pendingAnswer && !superseded && (
          <span style={{ color: 'var(--terminal-dim)' }}>· {tr('已回复', 'answered')}</span>
        )}
        <span style={{ color: 'var(--terminal-dim)', marginLeft: 'auto', fontVariantNumeric: 'tabular-nums' }}>
          {hhmmss(message.created_at)}
        </span>
      </div>
      <div style={{ marginTop: 5, color: 'var(--terminal-fg)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {message.text}
      </div>
      {options.length > 0 && (
        <div className="row gap6 wrap" style={{ marginTop: 7 }}>
          {options.map((opt) => (
            <span
              key={opt.id}
              style={{
                padding: '2px 9px',
                borderRadius: 999,
                border: '0.5px solid var(--terminal-border)',
                color: 'var(--terminal-fg)',
                fontSize: 10.5,
                opacity: 0.9,
              }}
            >
              {askOptionLabel(opt, lang)}
            </span>
          ))}
        </div>
      )}
      {ctx && (
        <>
          <button
            onClick={() => setOpen((o) => !o)}
            style={{
              marginTop: 6,
              border: 'none',
              background: 'transparent',
              cursor: 'pointer',
              padding: 0,
              fontSize: 11,
              fontFamily: 'var(--mono)',
              color: 'var(--terminal-accent)',
            }}
          >
            {open ? tr('收起诊断上下文', 'Hide context') : tr('展开诊断上下文', 'Show context')}
          </button>
          {open && (
            <div
              style={{
                marginTop: 5,
                color: 'var(--terminal-dim)',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontSize: 11,
                maxHeight: 260,
                overflowY: 'auto',
              }}
            >
              {ctx}
            </div>
          )}
        </>
      )}
      {pendingAnswer && (
        <div style={{ marginTop: 7, color: 'var(--terminal-warn)', fontSize: 11 }}>
          {tr('↓ 在下方输入框回复后任务会继续', '↓ Reply in the box below to continue the task')}
        </div>
      )}
    </div>
  );
}

/** 用户消息（建议 / 回答）：右对齐块。 */
export function UserNoteBlock({ message }: { message: VoyageMessageRead }) {
  const isAnswer = message.kind === 'answer';
  const consumed = message.kind === 'chat' && !!message.consumed_at;
  return (
    <div className="row" style={{ justifyContent: 'flex-end', margin: '6px 0' }}>
      <div
        style={{
          maxWidth: '80%',
          padding: '7px 10px',
          background: 'var(--terminal-bg-2)',
          borderLeft: '2.5px solid var(--terminal-accent)',
          borderRadius: 8,
        }}
      >
        <div className="row" style={{ gap: 6 }}>
          <span style={{ color: 'var(--terminal-accent)', fontWeight: 700 }}>
            {isAnswer ? tr('你的回复', 'Your reply') : tr('你的建议', 'Your suggestion')}
          </span>
          {consumed && (
            <span style={{ color: 'var(--terminal-dim)', fontSize: 10.5 }}>
              · {tr('AI 已采纳', 'picked up')}
            </span>
          )}
          <span style={{ color: 'var(--terminal-dim)', marginLeft: 'auto', fontVariantNumeric: 'tabular-nums' }}>
            {hhmmss(message.created_at)}
          </span>
        </div>
        {message.text && (
          <div style={{ marginTop: 3, color: 'var(--terminal-fg)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {message.text}
          </div>
        )}
      </div>
    </div>
  );
}

/** AI 播报（info）：一行弱色说明。 */
export function InfoNoteLine({ message }: { message: VoyageMessageRead }) {
  return (
    <div className="row" style={{ gap: 8, alignItems: 'flex-start', padding: '1px 0' }}>
      <span style={{ color: 'var(--terminal-dim)', flexShrink: 0, fontVariantNumeric: 'tabular-nums' }}>
        {hhmmss(message.created_at)}
      </span>
      <span style={{ color: 'var(--terminal-plan)', whiteSpace: 'pre-wrap', wordBreak: 'break-word', minWidth: 0 }}>
        <Icon name="sparkle" size={10} style={{ display: 'inline-block', verticalAlign: '-1px', marginRight: 4 }} />
        {message.text}
      </span>
    </div>
  );
}

/** 消息 → 终端混排块（未知 kind 返回 null，调用方跳过）。 */
export function messageNode(message: VoyageMessageRead) {
  if (message.kind === 'ask') return <AskBlock message={message} />;
  if (message.kind === 'chat' || message.kind === 'answer') return <UserNoteBlock message={message} />;
  if (message.kind === 'info') return <InfoNoteLine message={message} />;
  return null;
}
