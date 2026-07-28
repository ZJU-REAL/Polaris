import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api, VOYAGE_TERMINAL, type VoyageRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { ConfirmModal } from './ConfirmModal';
import { Icon } from './Icon';
import { toast } from './Toast';

/* ============================================================
   任务的三个动作，按状态给：

   - 还在跑 → 取消（协作式：引擎在下一步边界退出）
   - 因错误暂停 → 续跑（从断点继续，已完成的步骤不重跑）
   - 已结束 → 删除

   删除只对已结束的开放：还在跑就删的话，worker 那边仍在按这个 id 执行，行没了会
   一路报到不知所云的地方。要删先取消。
   ============================================================ */

export function VoyageActions({
  voyage,
  onDone,
  compact,
  showResume = true,
}: {
  voyage: VoyageRead;
  /** 删除成功后的回调（详情页用来跳回列表）。 */
  onDone?: () => void;
  /** 列表行里用：只出图标，不占宽度。 */
  compact?: boolean;
  /** 详情页把「续跑」放在报错说明旁边（那里更好理解），这里就别再出一个。 */
  showResume?: boolean;
}) {
  const queryClient = useQueryClient();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const finished = VOYAGE_TERMINAL.has(voyage.status);
  const canResume = showResume && voyage.status === 'paused_error';

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    void queryClient.invalidateQueries({ queryKey: ['voyage', voyage.id] });
  };

  const cancel = useMutation({
    mutationFn: () => api.cancelVoyage(voyage.id),
    onSuccess: () => { toast(tr('任务已取消', 'Task cancelled'), 'ok'); invalidate(); },
    onError: (e) => toast(`${tr('取消失败', 'Cancel failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const resume = useMutation({
    mutationFn: () => api.resumeVoyage(voyage.id),
    onSuccess: () => {
      toast(tr('已从断点继续，已完成的步骤不会重跑', 'Resumed from the last checkpoint — finished steps will not rerun'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('续跑失败', 'Resume failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteVoyage(voyage.id),
    onSuccess: () => { toast(tr('任务已删除', 'Task deleted'), 'ok'); invalidate(); onDone?.(); },
    onError: (e) => toast(`${tr('删除失败', 'Delete failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const busy = cancel.isPending || resume.isPending || remove.isPending;
  const size = compact ? 12 : 13;
  const cls = compact ? 'btn btn-ghost sm' : 'btn btn-ghost';

  return (
    <span className="row gap6" onClick={(e) => e.stopPropagation()}>
      {canResume && (
        <button type="button" className={cls} disabled={busy} onClick={() => resume.mutate()}
                title={tr('从断点继续，已完成的步骤不会重跑', 'Resume from the checkpoint — finished steps will not rerun')}>
          <Icon name="play" size={size} />
          {!compact && tr('续跑', 'Resume')}
        </button>
      )}
      {!finished && (
        <button type="button" className={cls} disabled={busy} onClick={() => cancel.mutate()}
                title={tr('取消任务', 'Cancel task')}>
          <Icon name="x" size={size} />
          {!compact && tr('取消', 'Cancel')}
        </button>
      )}
      {finished && (
        <button type="button" className={cls} disabled={busy} onClick={() => setConfirmDelete(true)}
                title={tr('删除任务记录', 'Delete task record')}>
          <Icon name="trash" size={size} />
          {!compact && tr('删除', 'Delete')}
        </button>
      )}
      <ConfirmModal
        open={confirmDelete}
        title={tr('删除这个任务？', 'Delete this task?')}
        message={tr(
          '步骤与日志会一并删除，无法撤销。它花过的 token 用量会留在账上，不受影响。',
          'Its steps and logs go with it and cannot be recovered. The tokens it spent stay on the books.',
        )}
        confirmText={tr('删除', 'Delete')}
        danger
        onConfirm={() => { setConfirmDelete(false); remove.mutate(); }}
        onClose={() => setConfirmDelete(false)}
      />
    </span>
  );
}
