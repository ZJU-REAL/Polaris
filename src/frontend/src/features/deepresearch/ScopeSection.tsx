import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import type { ResearchBrief, ResearchRunDetail } from '../../lib/researchApi';
import { BriefEditor } from './BriefEditor';
import { AttentionBlock, NowDoing, ProgressBar, RetryBlock, Section, StoppedBlock, type RunActions } from './parts';
import type { SectionState } from './phase';

/* ============================================================
   范围：Polaris 怎么理解这个方向、按什么去检索。
   确定之前是一句「正在确定」；确定之后是说明 + 检索词 + 来源与时间范围，
   并可以改一改重新检索。
   ============================================================ */

function windowText(months: number): string {
  if (months <= 12) return tr('近 1 年', 'past year');
  const years = Math.round(months / 12);
  return tr(`近 ${years} 年`, `past ${years} years`);
}

export interface ScopeSectionProps {
  run: ResearchRunDetail;
  state: SectionState;
  actions: RunActions;
  /** 范围编辑器是否打开（也会被「调整关键词」这个卡住时的动作打开） */
  editing: boolean;
  onCloseEdit: () => void;
  onSubmitEdit: (brief: ResearchBrief) => void;
}

export function ScopeSection({ run, state, actions, editing, onCloseEdit, onSubmitEdit }: ScopeSectionProps) {
  const brief = run.brief;
  // 来源 id → 给人看的名字（同 BriefEditor 的那份缓存）
  const { data: sourceOptions } = useQuery({
    queryKey: ['literature-sources'],
    queryFn: () => api.listLiteratureSources(),
    staleTime: 5 * 60_000,
    retry: false,
    enabled: !!brief,
  });
  const sourceName = (id: string) => sourceOptions?.find((s) => s.id === id)?.title ?? id;

  // 运行中不能改范围（改了也要等这一步停下）；其余时候都可以改了重新检索
  const canEdit = !!brief && !editing && run.status !== 'running';

  return (
    <Section
      title={tr('范围', 'Scope')}
      state={state}
      aside={
        canEdit ? (
          <button type="button" className="btn btn-plain xs" onClick={actions.editScope} disabled={actions.busy}>
            {tr('调整范围并重新检索', 'Edit scope and search again')}
          </button>
        ) : undefined
      }
    >
      {state === 'running' && (
        <>
          <ProgressBar fraction={null} label={tr('正在确定范围', 'Working out the scope')} />
          <NowDoing
            caption={
              brief
                ? // 范围是用户自己调过的：不用再推，只剩建文献库这一步
                  tr('正在按这个范围准备文献库', 'Setting up the library for this scope')
                : tr('正在根据你的方向确定检索范围', 'Working out what to search for from your direction')
            }
            wait={tr('通常不到一分钟。', 'Usually under a minute.')}
          />
        </>
      )}

      {state === 'retrying' && run.next_retry_at && (
        <RetryBlock nextRetryAt={run.next_retry_at} attempt={run.retry_attempt} lastError={run.last_error} actions={actions} />
      )}

      {state === 'attention' && run.attention && <AttentionBlock attention={run.attention} run={run} actions={actions} />}

      {state === 'stopped' && (
        <StoppedBlock text={tr('已在确定范围时停止。', 'Stopped while working out the scope.')} actions={actions} />
      )}

      {brief && !editing && (
        <div className="dr-scope">
          <p className="dr-scope-statement">{brief.statement}</p>
          <div className="dr-tags">
            {brief.include_keywords.map((k) => (
              <span key={k} className="tag">
                {k}
              </span>
            ))}
          </div>
          <div className="dr-meta">
            {[brief.sources.map(sourceName).join(tr('、', ', ')), windowText(brief.months_back)].filter(Boolean).join(' · ')}
          </div>
        </div>
      )}

      {brief && editing && (
        <BriefEditor
          brief={brief}
          hideName
          focus="keywords"
          submitLabel={tr('重新检索', 'Search again')}
          busyLabel={tr('正在开始…', 'Starting…')}
          busy={actions.busy}
          onSubmit={onSubmitEdit}
          onCancel={onCloseEdit}
        />
      )}
    </Section>
  );
}
