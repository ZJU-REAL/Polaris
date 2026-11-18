import { Link } from 'react-router-dom';
import { tr } from '../../lib/i18n';
import type { ResearchRunDetail } from '../../lib/researchApi';
import { AttentionBlock, NowDoing, PendingLine, ProgressBar, RetryBlock, Section, StoppedBlock, type RunActions } from './parts';
import { surveyFraction, surveyProgressText, surveyStage, type SectionState, type SurveyStage } from './phase';

/* ============================================================
   文献：检索 → 筛选 → 生成解读，三小步的进度和已经找到的论文。
   数字来自文献库本身，刷新、重启后不会倒退。
   ============================================================ */

/** 进行中多列几篇（看得出在找什么）；做完后只留前几篇，把位置让给后面的段 */
const SHOWN_ACTIVE = 5;
const SHOWN_DONE = 3;

function stageLabel(stage: SurveyStage): string {
  if (stage === 'search') return tr('检索', 'Search');
  if (stage === 'screen') return tr('筛选', 'Screen');
  return tr('生成解读', 'Summarise');
}

export interface PapersSectionProps {
  run: ResearchRunDetail;
  state: SectionState;
  actions: RunActions;
  /** 任务事件流里最近一行日志：正在做的具体事 */
  liveCaption: string | null;
}

export function PapersSection({ run, state, actions, liveCaption }: PapersSectionProps) {
  const { survey } = run;
  const p = survey.progress;
  const stage = surveyStage(p);
  const progressText = surveyProgressText(p);
  const libraryHref = run.library_id ? `/libraries/${run.library_id}` : null;
  const active = state === 'running' || state === 'retrying';
  const papers = survey.top_papers.slice(0, state === 'done' ? SHOWN_DONE : SHOWN_ACTIVE);
  const stages: SurveyStage[] = ['search', 'screen', 'summarise'];

  return (
    <Section
      title={tr('文献', 'Papers')}
      state={state}
      aside={
        survey.voyage_id ? (
          <Link className="btn btn-plain xs" to={`/voyages/${survey.voyage_id}`}>
            {tr('详细日志', 'Detailed log')}
          </Link>
        ) : undefined
      }
    >
      {state === 'pending' && (
        <PendingLine>{tr('范围确定后，在这里检索、筛选论文并生成解读。', 'Once the scope is set, papers are searched, screened and summarised here.')}</PendingLine>
      )}

      {active && (
        <div className="dr-steps" aria-label={tr('当前步骤', 'Current step')}>
          {stages.map((s, i) => (
            <span
              key={s}
              className={'dr-step' + (s === stage ? ' is-current' : stages.indexOf(stage) > i ? ' is-done' : '')}
            >
              {stageLabel(s)}
            </span>
          ))}
        </div>
      )}

      {/* 卡住时原因里已经带着数字，不再重复一遍；「这一步没完成」例外——要看停在哪 */}
      {progressText && state !== 'pending' && (state !== 'attention' || run.attention?.code === 'SURVEY_FAILED') && (
        <div className="dr-progress-text">{progressText}</div>
      )}

      {state === 'running' && (
        <>
          <ProgressBar fraction={surveyFraction(p)} label={stageLabel(stage)} />
          <NowDoing
            caption={liveCaption ?? survey.step_title}
            wait={tr('这一步通常要几十分钟。可以离开这个页面，进度会保留。', 'This usually takes tens of minutes. You can leave this page; progress is kept.')}
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

      {survey.notes.length > 0 && state !== 'pending' && (
        <ul className="dr-notes">
          {survey.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
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
          {state === 'done' && (
            <Link className="btn btn-plain sm" to={libraryHref}>
              {tr('添加论文', 'Add papers')}
            </Link>
          )}
        </div>
      )}
    </Section>
  );
}
