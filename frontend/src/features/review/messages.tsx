import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import type { ReviewMessageRead } from '../../lib/api';

/* ============================================================
   评审消息气泡（features/review 共享）：
   - 辩论视图：正方 / 反方 / 裁判 用不同颜色边框 + author_name
     徽标，裁判判决高亮；
   - 讨论视图：human 右侧蓝泡（display_name），agent 左侧灰泡。
   ============================================================ */

export type DebateRole = 'pro' | 'con' | 'judge' | 'other';

const JUDGE_KEYWORDS = ['裁判', 'judge', 'referee', '评委'];

function isJudgeName(name: string): boolean {
  const n = name.toLowerCase();
  return JUDGE_KEYWORDS.some((k) => n.includes(k));
}

/**
 * 按出场顺序给辩论 agent 分配角色：名字含「裁判/judge」→ judge；
 * 其余按首次出现顺序 → 正方、反方，更多则 other。
 */
export function classifyDebateAuthors(messages: ReviewMessageRead[]): Map<string, DebateRole> {
  const map = new Map<string, DebateRole>();
  let side = 0;
  for (const m of messages) {
    if (m.author_type !== 'agent' || map.has(m.author_name)) continue;
    if (isJudgeName(m.author_name)) {
      map.set(m.author_name, 'judge');
    } else {
      map.set(m.author_name, side === 0 ? 'pro' : side === 1 ? 'con' : 'other');
      side += 1;
    }
  }
  return map;
}

interface RoleStyle {
  zh: string;
  en: string;
  border: string;
  badgeBg: string;
  badgeTx: string;
  bg?: string;
}

const ROLE_STYLE: Record<DebateRole, RoleStyle> = {
  pro: { zh: '正方', en: 'Pro', border: 'var(--accent-soft-2)', badgeBg: 'var(--accent-soft)', badgeTx: 'var(--accent-text)' },
  con: { zh: '反方', en: 'Con', border: 'var(--danger-bg)', badgeBg: 'var(--danger-bg)', badgeTx: 'var(--danger-tx)' },
  judge: {
    zh: '裁判',
    en: 'Judge',
    border: 'var(--warn)',
    badgeBg: 'var(--warn-bg)',
    badgeTx: 'var(--warn-tx)',
    bg: 'var(--warn-bg)',
  },
  other: { zh: '评审', en: 'Reviewer', border: 'var(--border-2)', badgeBg: 'var(--surface-3)', badgeTx: 'var(--text-2)' },
};

/** 辩论逐轮记录气泡（round 分组由调用方负责）。 */
export function DebateBubble({ msg, role }: { msg: ReviewMessageRead; role: DebateRole }) {
  const s = ROLE_STYLE[role];
  const judge = role === 'judge';
  return (
    <div
      style={{
        border: `1px solid ${s.border}`,
        borderLeft: `3px solid ${judge ? 'var(--warn)' : s.border}`,
        borderRadius: 10,
        padding: '10px 14px',
        background: s.bg ?? 'var(--surface)',
        marginBottom: 8,
      }}
    >
      <div className="row gap8" style={{ marginBottom: 6 }}>
        <span className="pill sm" style={{ background: s.badgeBg, color: s.badgeTx }}>
          {tr(s.zh, s.en)}
        </span>
        <span style={{ fontSize: 12, fontWeight: 650 }}>{msg.author_name}</span>
        {msg.round !== null && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>round {msg.round}</span>
        )}
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginLeft: 'auto' }}>
          {fmtTime(msg.created_at)}
        </span>
      </div>
      {judge && (
        <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--warn-tx)', marginBottom: 4 }}>
          {tr('裁判判决', 'Judge verdict')}
        </div>
      )}
      <Markdown source={msg.content} style={{ fontSize: 12.5 }} />
    </div>
  );
}

/** 人机讨论气泡：human 右侧蓝泡、agent 左侧灰泡。 */
export function DiscussionBubble({ msg }: { msg: ReviewMessageRead }) {
  const human = msg.author_type === 'human';
  return (
    <div className="row" style={{ justifyContent: human ? 'flex-end' : 'flex-start', marginBottom: 10 }}>
      <div
        style={{
          maxWidth: '78%',
          borderRadius: 12,
          padding: '9px 13px',
          background: human ? 'var(--accent-soft)' : 'var(--surface-2)',
          border: `0.5px solid ${human ? 'var(--accent-soft-2)' : 'var(--border-2)'}`,
        }}
      >
        <div className="row gap8" style={{ marginBottom: 4, justifyContent: human ? 'flex-end' : 'flex-start' }}>
          <span style={{ fontSize: 11, fontWeight: 650, color: human ? 'var(--accent-text)' : 'var(--text-2)' }}>
            {msg.author_name}
          </span>
          <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>{fmtTime(msg.created_at)}</span>
        </div>
        <Markdown source={msg.content} style={{ fontSize: 12.5 }} />
      </div>
    </div>
  );
}
