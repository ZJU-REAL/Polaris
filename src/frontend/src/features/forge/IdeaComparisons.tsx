import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { LoadingState } from '../../components/ui/EmptyState';
import { api, type ReviewMessageRead, type ReviewSessionRead } from '../../lib/api';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { Markdown } from '../../lib/markdown';
import { classifyDebateAuthors } from '../review/messages';

/* ============================================================
   一个想法的对比记录：它和谁比过、谁赢了；点开一场看审稿人是怎么讨论的。
   以前要去另一页（想法评审 › 辩论记录）再从下拉框里把这个想法选出来。
   ============================================================ */

export interface MatchInfo {
  session: ReviewSessionRead;
  opponentId: string | null;
  /** 判了没有；没判完的（任务还在跑或失败了）为 false */
  decided: boolean;
  won: boolean;
}

/** 从一场对比的记录里取出对手和胜负。 */
export function matchInfo(session: ReviewSessionRead, ideaId: string): MatchInfo {
  const p = session.payload ?? {};
  const a = typeof p.idea_a === 'string' ? p.idea_a : null;
  const b = typeof p.idea_b === 'string' ? p.idea_b : null;
  const raw = typeof p.winner === 'string' ? p.winner : null;
  const winnerId = raw === 'a' ? a : raw === 'b' ? b : raw;
  return {
    session,
    opponentId: a === ideaId ? b : a,
    decided: !!winnerId,
    won: !!winnerId && winnerId === ideaId,
  };
}

function Transcript({ sessionId, decided }: { sessionId: string; decided: boolean }) {
  const query = useQuery({
    queryKey: ['session-messages', sessionId],
    queryFn: () => api.listSessionMessages(sessionId),
    retry: false,
  });
  const messages: ReviewMessageRead[] = query.data ?? [];
  const roles = useMemo(() => classifyDebateAuthors(messages), [messages]);
  if (query.isLoading) return <LoadingState compact rows={2} />;
  if (query.isError) return <div className="idea-empty">{tr('无法加载这场讨论', 'Couldn’t load this discussion')}</div>;
  if (messages.length === 0) return <div className="idea-empty">{tr('这场对比还没有讨论内容', 'No discussion in this comparison yet')}</div>;
  // 裁定放最前面：先看结论，再看是怎么讨论出来的
  // 审稿人的名字里不一定带「裁判」：判完的一场，最后一段发言就是裁定
  const last = messages[messages.length - 1];
  const named = messages.filter((m) => roles.get(m.author_name) === 'judge').map((m) => m.id);
  const verdicts = new Set(named.length > 0 ? named : decided && last && last.author_type === 'agent' ? [last.id] : []);
  const ordered = [...messages.filter((m) => verdicts.has(m.id)), ...messages.filter((m) => !verdicts.has(m.id))];
  return (
    <div>
      {ordered.map((m) => {
        return <Turn key={m.id} message={m} judge={verdicts.has(m.id)} />;
      })}
    </div>
  );
}

/** 一段发言。长的先收起来（模型常把两个想法的全文再抄一遍）。 */
function Turn({ message: m, judge }: { message: ReviewMessageRead; judge: boolean }) {
  const long = m.content.length > 420 || m.content.split('\n').length > 8;
  const [open, setOpen] = useState(false);
  return (
    <div className={'idea-turn' + (judge ? ' is-judge' : '')}>
      <div className="idea-turn-who">
        <b>{m.author_name}</b>
        {judge ? ` · ${tr('裁定', 'Verdict')}` : m.round !== null ? ` · ${tr(`第 ${m.round} 轮`, `Round ${m.round}`)}` : ''}
      </div>
      <div className={'idea-turn-text' + (long && !open ? ' is-clamped' : '')}>
        <Markdown source={m.content} style={{ fontSize: 13 }} />
      </div>
      {long && (
        <button type="button" className="step-toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? tr('收起', 'Show less') : tr('显示全部', 'Show all')}
        </button>
      )}
    </div>
  );
}

export function IdeaComparisons({
  matches,
  titleOf,
  initialOpen,
}: {
  matches: MatchInfo[];
  titleOf: (id: string | null) => string;
  /** 一进来就展开的那一场 */
  initialOpen?: string | null;
}) {
  const [open, setOpen] = useState<string | null>(initialOpen ?? null);
  return (
    <div className="idea-matches">
      {matches.map((m) => {
        const isOpen = open === m.session.id;
        return (
          <div key={m.session.id} className="idea-match" data-match={m.session.id}>
            <button type="button" className="idea-match-head" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : m.session.id)}>
              <span className="idea-match-result">
                <span className={'task-dot ' + (m.decided ? (m.won ? 'is-ok' : 'is-err') : '')} />
                {m.decided ? (m.won ? tr('胜出', 'Won') : tr('落选', 'Lost')) : tr('未判定', 'Undecided')}
              </span>
              <span className="idea-match-opp" title={titleOf(m.opponentId)}>
                <span>{tr('对比 ', 'vs ')}</span>
                {titleOf(m.opponentId)}
              </span>
              <span className="idea-match-when">{fmtTime(m.session.created_at)}</span>
              <Icon name="chevDown" size={12} style={{ color: 'var(--text-3)', flexShrink: 0, transform: isOpen ? 'rotate(180deg)' : undefined }} />
            </button>
            {isOpen && (
              <div className="idea-match-body">
                <Transcript sessionId={m.session.id} decided={m.decided} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
