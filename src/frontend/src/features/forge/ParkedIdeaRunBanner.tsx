import { useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type ForgeParkedRun } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { ParkedRunNotice } from '../voyages/shared/ParkedRunNotice';

/* ============================================================
   想法页顶部的提醒条：有一条想法类任务停着等人处理。

   停着的任务不再算「进行中」（不挡新任务），所以「正在生成想法」那条提示不会为它
   出现——以前一条出错停住的生成任务在这一页上无声无息，只能去任务列表里碰见。
   - 出错暂停：为什么停、会不会自己再试，重试 / 查看任务 / 取消；
   - 等回答 / 等确认：去任务页处理，或者取消。
   ============================================================ */

/** 任务种类 → 这一页上的叫法（函数里求值：tr 不能在模块顶层调）。 */
export function parkedKindLabel(kind: string): string {
  if (kind === 'idea_review') return tr('想法评审', 'Idea review');
  if (kind === 'idea_proposal') return tr('深度生成', 'Deep dive');
  return tr('生成想法', 'Idea generation');
}

export function parkedTitle(run: Pick<ForgeParkedRun, 'kind' | 'status'>): string {
  const kind = parkedKindLabel(run.kind);
  if (run.status === 'paused_ask') return tr(`「${kind}」任务有问题要问你`, `The “${kind}” task has a question for you`);
  if (run.status === 'paused_gate') return tr(`「${kind}」任务在等你确认`, `The “${kind}” task is waiting for your approval`);
  return tr(`「${kind}」任务出错后暂停了`, `The “${kind}” task paused after an error`);
}

export function ParkedIdeaRunBanner({ run, pid }: { run: ForgeParkedRun; pid: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
    void queryClient.invalidateQueries({ queryKey: ['deep-state', pid] });
    void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    void queryClient.invalidateQueries({ queryKey: ['voyage', run.voyage_id] });
  };
  const resume = useMutation({
    mutationFn: () => api.resumeVoyage(run.voyage_id),
    onSuccess: () => {
      toast(tr('已从出错的步骤继续', 'Resumed from the failed step'), 'ok');
      refresh();
    },
    onError: (e) => toast(`${tr('无法重试：', 'Couldn’t retry: ')}${errorText(e)}`, 'error'),
  });
  const cancel = useMutation({
    mutationFn: () => api.cancelVoyage(run.voyage_id),
    onSuccess: () => {
      toast(tr('任务已取消', 'Task cancelled'), 'ok');
      refresh();
    },
    onError: (e) => toast(`${tr('无法取消任务：', 'Couldn’t cancel the task: ')}${errorText(e)}`, 'error'),
  });
  const busy = resume.isPending || cancel.isPending;

  const secondary = (
    <>
      <button className="btn btn-ghost sm" disabled={busy} onClick={() => cancel.mutate()}>
        {tr('取消任务', 'Cancel task')}
      </button>
      <button className="btn btn-ghost sm" onClick={() => navigate(`/voyages/${run.voyage_id}`)}>
        {tr('查看任务', 'View task')}
      </button>
    </>
  );

  if (run.status === 'paused_error') {
    return (
      <div style={{ marginBottom: 16 }} data-testid="parked-idea-run">
        <ParkedRunNotice
          title={parkedTitle(run)}
          error={{ code: run.error_code, message: run.error_message, detail: run.error_detail }}
          retryAt={run.retry_at}
          retryAttempt={run.retry_attempt}
          onRetry={() => resume.mutate()}
          retrying={resume.isPending}
          extraActions={secondary}
        />
      </div>
    );
  }

  return (
    <div
      data-testid="parked-idea-run"
      className="row gap10"
      style={{
        marginBottom: 16,
        padding: '12px 14px',
        background: 'var(--warn-bg)',
        color: 'var(--warn-tx)',
        borderRadius: 10,
        fontSize: 13,
        flexWrap: 'wrap',
      }}
    >
      <Icon name={run.status === 'paused_ask' ? 'sparkle' : 'gate'} size={14} style={{ flexShrink: 0 }} />
      <span style={{ fontWeight: 600, flex: '1 1 240px', minWidth: 0 }}>
        {parkedTitle(run)}
        <span style={{ fontWeight: 400 }}>
          {run.status === 'paused_ask'
            ? tr('，回复后任务会继续。', '. It continues once you reply.')
            : tr('，批准后任务会继续。', '. It continues once you approve.')}
        </span>
      </span>
      <div className="row gap8" style={{ marginLeft: 'auto', flexShrink: 0 }}>
        <button className="btn btn-ghost sm" disabled={busy} onClick={() => cancel.mutate()}>
          {tr('取消任务', 'Cancel task')}
        </button>
        <button className="btn btn-primary sm" onClick={() => navigate(`/voyages/${run.voyage_id}`)}>
          {run.status === 'paused_ask' ? tr('去回复', 'Reply') : tr('去处理', 'Review')}
        </button>
      </div>
    </div>
  );
}
