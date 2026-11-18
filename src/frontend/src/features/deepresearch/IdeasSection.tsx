import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { researchApi, type ResearchIdea, type ResearchRunDetail } from '../../lib/researchApi';
import { researchKeys } from './hooks';
import { IdeaEvidence } from './IdeaEvidence';
import { InlineQuestion } from './InlineQuestion';
import { AttentionBlock, NowDoing, PendingLine, ProgressBar, RetryBlock, Section, StoppedBlock, type RunActions } from './parts';
import { evidenceText, type SectionState } from './phase';

/* ============================================================
   想法：寻找假设的进度、AI 的提问、以及活下来的想法（每个都带文献依据的数量）。
   用户在这里做整条流程里唯一必须做的决定：选定哪些想法继续做。
   ============================================================ */

function scoresText(idea: ResearchIdea): string {
  const s = idea.scores;
  if (!s) return '';
  return [
    tr(`新颖性 ${s.novelty}`, `Novelty ${s.novelty}`),
    tr(`可行性 ${s.feasibility}`, `Feasibility ${s.feasibility}`),
    tr(`影响 ${s.impact}`, `Impact ${s.impact}`),
  ].join(' · ');
}

export interface IdeasSectionProps {
  run: ResearchRunDetail;
  state: SectionState;
  actions: RunActions;
  liveCaption: string | null;
  onMoreIdeas: () => void;
  onChanged: () => void;
}

export function IdeasSection({ run, state, actions, liveCaption, onMoreIdeas, onChanged }: IdeasSectionProps) {
  const queryClient = useQueryClient();
  const { hypotheses: h, ideas } = run;
  const picked = ideas.filter((i) => i.status === 'promoted').length;
  const choosing = state === 'waiting' && run.phase === 'choose';
  // 新颖性只对照了这一个文献库：只要显示了分数，这句话就要在
  const showNovelty = ideas.some((i) => i.scores);

  const pick = useMutation({
    mutationFn: (v: { ideaId: string; picked: boolean }) => researchApi.pickIdea(v.ideaId, v.picked),
    // 先改界面再等回应：选定是高频、可逆的动作，不该等一次往返
    onMutate: async (v) => {
      const key = researchKeys.run(run.id);
      await queryClient.cancelQueries({ queryKey: key });
      const prev = queryClient.getQueryData<ResearchRunDetail>(key);
      if (prev) {
        const next = prev.ideas.map((i) => (i.id === v.ideaId ? { ...i, status: v.picked ? ('promoted' as const) : ('candidate' as const) } : i));
        queryClient.setQueryData<ResearchRunDetail>(key, {
          ...prev,
          ideas: next,
          counts: { ...prev.counts, picked: next.filter((i) => i.status === 'promoted').length },
        });
      }
      return { prev };
    },
    onSuccess: (_d, v) => {
      if (v.picked) toast(tr('已选定', 'Picked'), 'ok');
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(researchKeys.run(run.id), ctx.prev);
      toast(errorText(e), 'error');
    },
    onSettled: onChanged,
  });

  return (
    <Section
      title={tr('想法', 'Ideas')}
      state={state}
      stateLabel={
        state === 'waiting' ? (choosing ? tr('等你选定', 'Waiting for your picks') : tr('等你回答', 'Waiting for your answer')) : undefined
      }
      aside={
        h.voyage_id ? (
          <Link className="btn btn-plain xs" to={`/voyages/${h.voyage_id}`}>
            {tr('查看探索过程', 'View the search')}
          </Link>
        ) : undefined
      }
    >
      {state === 'pending' && (
        <PendingLine>
          {tr('文献读完后，在这里寻找有文献依据的假设，留下来的会成为想法。', 'Once the papers are read, hypotheses the papers support are looked for here. The ones that hold up become ideas.')}
        </PendingLine>
      )}

      {run.open_ask && h.voyage_id && state === 'waiting' && (
        <InlineQuestion voyageId={h.voyage_id} ask={run.open_ask} onAnswered={onChanged} />
      )}

      {(state === 'running' || state === 'retrying' || (state === 'waiting' && !choosing)) && h.max_rounds > 0 && h.round > 0 && (
        <div className="dr-progress-text">
          {tr(`第 ${h.round}/${h.max_rounds} 轮 · ${h.node_count} 个假设`, `Round ${h.round} of ${h.max_rounds} · ${h.node_count} ${h.node_count === 1 ? 'hypothesis' : 'hypotheses'}`)}
        </div>
      )}

      {state === 'running' && (
        <>
          <ProgressBar
            fraction={h.max_rounds > 0 && h.round > 0 ? (h.round - 0.5) / h.max_rounds : null}
            label={tr('寻找假设', 'Looking for hypotheses')}
          />
          <NowDoing
            caption={liveCaption}
            wait={tr('每一轮提出假设、对照文献核对、淘汰站不住的。通常要几十分钟，可以离开这个页面。', 'Each round proposes hypotheses, checks them against the papers and drops the ones that don’t hold. This usually takes tens of minutes; you can leave this page.')}
          />
        </>
      )}

      {state === 'retrying' && run.next_retry_at && (
        <RetryBlock nextRetryAt={run.next_retry_at} attempt={run.retry_attempt} lastError={run.last_error} actions={actions} />
      )}

      {state === 'attention' && run.attention && <AttentionBlock attention={run.attention} run={run} actions={actions} />}

      {state === 'stopped' && (
        <StoppedBlock
          text={
            ideas.length > 0
              ? tr('已停止。已有的想法都保留着。', 'Stopped. The ideas found so far are kept.')
              : tr('已在寻找假设时停止。', 'Stopped while looking for hypotheses.')
          }
          actions={actions}
        />
      )}

      {ideas.length > 0 && (
        <>
          <div className="dr-ideas-head">
            <span className="dr-ideas-lead">
              {choosing
                ? tr('选出你想继续做的想法', 'Pick the ideas you want to pursue')
                : picked > 0
                  ? tr(`已选定 ${picked} 个，共 ${ideas.length} 个想法`, `${picked} of ${ideas.length} ${ideas.length === 1 ? 'idea' : 'ideas'} picked`)
                  : tr(`${ideas.length} 个想法`, `${ideas.length} ${ideas.length === 1 ? 'idea' : 'ideas'}`)}
            </span>
            {showNovelty && (
              <span className="dr-ideas-note">{tr('新颖性只对照你的文献库判断。', 'Novelty is judged against your library only.')}</span>
            )}
          </div>
          <ul className="dr-ideas">
            {ideas.map((idea) => (
              <IdeaCard
                key={idea.id}
                idea={idea}
                busy={pick.isPending && pick.variables?.ideaId === idea.id}
                onPick={(p) => pick.mutate({ ideaId: idea.id, picked: p })}
              />
            ))}
          </ul>
          <div className="dr-links">
            {state !== 'running' && state !== 'retrying' && (
              <button type="button" className="btn btn-ghost sm" onClick={onMoreIdeas} disabled={actions.busy}>
                {tr('再生成一批', 'Generate more')}
              </button>
            )}
            <Link className="btn btn-plain sm" to={`/t/${run.project_id}/forge`}>
              {tr('全部想法', 'All ideas')}
            </Link>
            {ideas.length >= 2 && (
              <Link className="btn btn-plain sm" to={`/t/${run.project_id}/review`}>
                {tr('对比排序', 'Compare')}
              </Link>
            )}
          </div>
        </>
      )}
    </Section>
  );
}

function IdeaCard({ idea, busy, onPick }: { idea: ResearchIdea; busy: boolean; onPick: (picked: boolean) => void }) {
  const [open, setOpen] = useState(false);
  const picked = idea.status === 'promoted';
  const scores = scoresText(idea);
  return (
    <li className={'dr-idea' + (picked ? ' is-picked' : '')}>
      <div className="dr-idea-main">
        <Link to={`/ideas/${idea.id}`} className="dr-idea-title" title={idea.title}>
          {idea.title}
        </Link>
        <p className="dr-idea-summary">{idea.summary}</p>
        <div className="dr-idea-meta">
          <button type="button" className="dr-idea-evidence" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
            {evidenceText(idea.evidence_counts)}
            <Icon name="chevDown" size={11} style={{ transform: open ? 'rotate(180deg)' : undefined }} />
          </button>
          {scores && <span className="dr-idea-scores">{scores}</span>}
        </div>
        {open && <IdeaEvidence ideaId={idea.id} unsupported={idea.evidence_counts.unsupported} />}
      </div>
      <div className="dr-idea-act">
        {picked ? (
          <>
            <span className="dr-idea-picked">
              <Icon name="check" size={12} sw={2.2} />
              {tr('已选定', 'Picked')}
            </span>
            <button type="button" className="btn btn-plain xs" disabled={busy} onClick={() => onPick(false)}>
              {tr('取消选定', 'Unpick')}
            </button>
          </>
        ) : (
          <button type="button" className="btn btn-ghost sm" disabled={busy} onClick={() => onPick(true)}>
            {tr('选定', 'Pick')}
          </button>
        )}
      </div>
    </li>
  );
}
