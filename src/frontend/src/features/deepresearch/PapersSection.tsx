import { useState } from 'react';
import { Link } from 'react-router-dom';
import { plainSummary } from '../../lib/format';
import { tr } from '../../lib/i18n';
import type { ResearchRunDetail } from '../../lib/researchApi';
import { useNow, useResearchEstimate } from './hooks';
import { AttentionBlock, NowDoing, PendingLine, ProgressBar, RetryBlock, Section, StoppedBlock, type RunActions } from './parts';
import {
  costText,
  elapsedText,
  estimateText,
  groupQueries,
  querySortText,
  queryWindowText,
  sourceLines,
  sourceName,
  surveyDoneText,
  surveyFraction,
  surveyFunnelText,
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
  const queryGroupsAll = groupQueries(survey.queries);
  // 检索过但一条都没返回的来源，引擎的来源清单里没有它：照实补上「0 条」，不让它悄悄消失
  const silent = queryGroupsAll
    .filter((g) => !(survey.sources ?? []).some((x) => x.source === g.source))
    .map((g) => ({ source: g.source, fetched: g.returned, error: g.queries.every((q) => q.error) ? (g.queries[0]?.error ?? null) : null }));
  const sources = sourceLines([...(survey.sources ?? []), ...silent]);
  const briefSources = (run.brief?.sources ?? []).map(sourceName);
  const queryGroups = queryGroupsAll;
  const queryCount = queryGroups.reduce((n, g) => n + g.queries.length, 0);
  const [showQueries, setShowQueries] = useState(false);
  const funnel = state === 'done' ? surveyFunnelText(p) : '';
  // 检索结果比取进来筛选的多：照实说只取了最相关的那一部分（上限在范围里）
  const cap = run.brief?.max_papers ?? 0;
  const fetchedTotal = p.fetched ?? (survey.sources ?? []).reduce((n, s) => n + s.fetched, 0);
  const hitCap = cap > 0 && fetchedTotal > cap && (survey.sources ?? []).some((s) => typeof s.selected === 'number');
  const estimate = useResearchEstimate(state === 'running');
  const spent = [started ? elapsedText(survey.started_at, active ? null : survey.finished_at, now) : '', started ? costText(survey.usage) : '']
    .filter(Boolean)
    .join(' · ');

  return (
    <Section
      title={tr('文献', 'Papers')}
      state={state}
      meta={spent || undefined}
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
        <div className="dr-progress-text" data-testid="survey-result">{progressText}</div>
      )}
      {funnel && (
        <div className="dr-funnel" data-testid="survey-funnel">
          {funnel}
        </div>
      )}

      {state === 'running' && (
        <>
          <ProgressBar fraction={surveyFraction(p, stage)} label={surveyStageLabel(stage)} />
          <NowDoing
            caption={surveyStageCaption(stage, p, briefSources)}
            wait={tr(
              `一次研究${estimateText(estimate)}。可以离开这个页面，进度会保留。`,
              `A research run ${estimateText(estimate).replace(/^U/, 'u')}. You can leave this page; progress is kept.`,
            )}
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
          {queryCount > 0 && (
            <button
              type="button"
              className="dr-queries-toggle"
              aria-expanded={showQueries}
              data-testid="queries-toggle"
              onClick={() => setShowQueries((v) => !v)}
            >
              {showQueries ? tr('收起检索式', 'Hide search queries') : tr('查看检索式', 'Show search queries')}
            </button>
          )}
        </div>
      )}

      {showQueries && queryCount > 0 && (
        <div className="dr-queries" data-testid="queries">
          <p className="dr-queries-note">
            {tr(
              `向 ${queryGroups.length} 个来源发了 ${queryCount} 次检索。每个来源返回的结果有重复，合并后再逐篇判断是否相关。`,
              `${queryCount} searches were sent to ${queryGroups.length} ${queryGroups.length === 1 ? 'source' : 'sources'}. Results overlap; they are merged, then each paper is checked for relevance.`,
            )}
            {hitCap && tr(` 每次检索最多取 ${cap} 篇进来筛选，可以在范围里调高。`, ` At most ${cap} papers per search are screened; raise it in the scope.`)}
          </p>
          {queryGroups.map((g) => {
            const src = (survey.sources ?? []).find((x) => x.source === g.source);
            return (
              <div key={g.source} className="dr-query-group">
                <div className="dr-query-source">
                  {g.name}
                  <span>
                    {tr(`${g.queries.length} 次检索，返回 ${g.returned} 条`, `${g.queries.length} ${g.queries.length === 1 ? 'search' : 'searches'}, ${g.returned} results`)}
                    {typeof src?.selected === 'number' && tr(`，取 ${src.selected} 篇筛选`, `, ${src.selected} screened`)}
                  </span>
                </div>
                <ul>
                  {g.queries.map((q, i) => (
                    <li key={i} className={q.error ? 'is-failed' : undefined}>
                      <code title={q.query}>{q.query}</code>
                      <span className="dr-query-meta">{[querySortText(q.sort), queryWindowText(q.window)].filter(Boolean).join(tr('，', ', '))}</span>
                      <span className="dr-query-n" title={q.error ?? undefined}>
                        {q.error ? tr('没取到', 'failed') : tr(`${q.returned} 条`, String(q.returned))}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
      )}

      {papers.length > 0 && (
        <ol className="dr-papers">
          {papers.map((paper) => (
            <li key={paper.id}>
              <Link to={`/papers/${paper.id}/read`} className="dr-paper-title" title={paper.title}>
                {paper.title}
              </Link>
              <div className="dr-paper-tldr" title={plainSummary(paper.tldr) || undefined}>
                {[paper.year, plainSummary(paper.tldr)].filter(Boolean).join(' · ')}
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
