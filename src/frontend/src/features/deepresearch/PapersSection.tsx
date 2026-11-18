import { Link } from 'react-router-dom';
import { tr } from '../../lib/i18n';
import type { ResearchRunDetail } from '../../lib/researchApi';
import { useNow } from './hooks';
import { AttentionBlock, NowDoing, PendingLine, ProgressBar, RetryBlock, Section, StoppedBlock, type RunActions } from './parts';
import {
  elapsedText,
  sourceLines,
  sourceName,
  surveyDoneText,
  surveyFraction,
  surveyProgressText,
  surveyStage,
  surveyStageCaption,
  surveyStageLabel,
  SURVEY_STAGES,
  type SectionState,
} from './phase';

/* ============================================================
   文献：检索 → 筛选 → 读全文 → 写解读 → 整理，五小步的进度和已经找到的论文。
   数字来自文献库本身，刷新、重启后不会倒退；各来源拿到多少、哪个没连上照实列出。
   ============================================================ */

/** 进行中多列几篇（看得出在找什么）；做完后只留前几篇，把位置让给后面的段 */
const SHOWN_ACTIVE = 5;
const SHOWN_DONE = 3;

export interface PapersSectionProps {
  run: ResearchRunDetail;
  state: SectionState;
  actions: RunActions;
}

export function PapersSection({ run, state, actions }: PapersSectionProps) {
  const { survey } = run;
  const p = survey.progress;
  const stage = surveyStage(p, survey.step_action, survey.state);
  const progressText = state === 'done' ? surveyDoneText(p) : surveyProgressText(p);
  const libraryHref = run.library_id ? `/libraries/${run.library_id}` : null;
  const active = state === 'running' || state === 'retrying';
  const papers = survey.top_papers.slice(0, state === 'done' ? SHOWN_DONE : SHOWN_ACTIVE);
  const now = useNow(30_000, active);
  // 花了多久：进行中数到现在，结束后是定数；卡住、停下时任务没有结束时刻，不写
  const started = state !== 'pending' && !!survey.started_at && (active || !!survey.finished_at);
  const sources = sourceLines(survey.sources ?? []);
  const briefSources = (run.brief?.sources ?? []).map(sourceName);
  // 检索到的篇数碰到了上限：照实说还可能有没取到的
  const cap = run.brief?.max_papers ?? 0;
  const fetchedTotal = (survey.sources ?? []).reduce((n, s) => n + s.fetched, 0);
  const hitCap = cap > 0 && fetchedTotal >= cap;

  return (
    <Section
      title={tr('文献', 'Papers')}
      state={state}
      meta={started ? elapsedText(survey.started_at, active ? null : survey.finished_at, now) : undefined}
      aside={
        survey.voyage_id ? (
          <Link className="btn btn-plain xs" to={`/voyages/${survey.voyage_id}`}>
            {tr('查看过程', 'View steps')}
          </Link>
        ) : undefined
      }
    >
      {state === 'pending' && (
        <PendingLine>{tr('范围确定后，在这里检索、筛选论文并写解读。', 'Once the scope is set, papers are searched, screened and summarised here.')}</PendingLine>
      )}

      {active && (
        <ol className="dr-steps" aria-label={tr('当前步骤', 'Current step')}>
          {SURVEY_STAGES.map((s, i) => (
            <li
              key={s}
              className={'dr-step' + (s === stage ? ' is-current' : SURVEY_STAGES.indexOf(stage) > i ? ' is-done' : '')}
              aria-current={s === stage ? 'step' : undefined}
            >
              {surveyStageLabel(s)}
            </li>
          ))}
        </ol>
      )}

      {/* 卡住时原因里已经带着数字，不再重复一遍；「这一步没完成」例外——要看停在哪 */}
      {progressText && state !== 'pending' && (state !== 'attention' || run.attention?.code === 'SURVEY_FAILED') && (
        <div className="dr-progress-text">{progressText}</div>
      )}

      {state === 'running' && (
        <>
          <ProgressBar fraction={surveyFraction(p, stage)} label={surveyStageLabel(stage)} />
          <NowDoing
            caption={surveyStageCaption(stage, p, briefSources)}
            wait={tr('整个过程通常要几十分钟。可以离开这个页面，进度会保留。', 'The whole thing usually takes tens of minutes. You can leave this page; progress is kept.')}
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
            p.kept > 0
              ? tr(`已停止。已留下的 ${p.kept} 篇论文保留在文献库里。`, `Stopped. The ${p.kept} papers kept so far stay in the library.`)
              : tr('已在检索文献时停止。', 'Stopped while finding papers.')
          }
          actions={actions}
        />
      )}

      {sources.length > 0 && state !== 'pending' && (
        <div className="dr-sources">
          <span className="dr-sources-label">{tr('来源', 'Sources')}</span>
          {sources.map((s) => (
            <span key={s.id} className={'dr-source' + (s.failed ? ' is-failed' : '')} title={s.detail ?? undefined}>
              {s.name} {s.text}
            </span>
          ))}
          {hitCap && (
            <span className="dr-source">{tr(`已到 ${cap} 篇上限，可能还有没取到的`, `Reached the ${cap}-paper limit; there may be more`)}</span>
          )}
        </div>
      )}

      {papers.length > 0 && (
        <ol className="dr-papers">
          {papers.map((paper) => (
            <li key={paper.id}>
              <Link to={`/papers/${paper.id}/read`} className="dr-paper-title" title={paper.title}>
                {paper.title}
              </Link>
              <div className="dr-paper-tldr" title={paper.tldr ?? undefined}>
                {[paper.year, paper.tldr].filter(Boolean).join(' · ')}
              </div>
            </li>
          ))}
        </ol>
      )}

      {libraryHref && p.kept > 0 && (
        <div className="dr-links">
          <Link className="btn btn-ghost sm" to={libraryHref}>
            {tr(`查看全部 ${p.kept} 篇`, p.kept === 1 ? 'View the paper' : `View all ${p.kept} papers`)}
          </Link>
        </div>
      )}
    </Section>
  );
}
