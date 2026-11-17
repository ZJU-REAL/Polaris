import { Icon } from '../../components/ui/Icon';
import type { IdeaStatus } from '../../lib/api';
import { tr } from '../../lib/i18n';

export interface IdeaActionCardProps {
  status: IdeaStatus;
  /** 有状态请求在途：按钮全部禁用，避免连点。 */
  busy?: boolean;
  onPick: () => void;
  onUnpick: () => void;
  onReject: () => void;
  onRestore: () => void;
  onStartExperiment: () => void;
}

/** 想法页右栏的「下一步」：每种状态一个主操作。

    - 候选 / 评审中：选中（主）· 淘汰
    - 已选中：开始实验（主）· 取消选中
    - 已淘汰：恢复为候选

    选中直接生效，没有审批；以前选中之后这里只剩一句「已晋级」和两个禁用按钮。 */
export function IdeaActionCard({
  status,
  busy,
  onPick,
  onUnpick,
  onReject,
  onRestore,
  onStartExperiment,
}: IdeaActionCardProps) {
  const picked = status === 'promoted';
  const rejected = status === 'rejected';
  const hint = picked
    ? tr('已选中，可以为它开始实验。', 'Picked. You can start an experiment for it.')
    : rejected
      ? tr('已淘汰，不再参与评审和实验。', 'Rejected. It’s left out of review and experiments.')
      : tr('选中值得继续做的想法，再为它开始实验。', 'Pick the ideas worth pursuing, then start an experiment.');
  const full = { flex: 1, justifyContent: 'center' } as const;
  return (
    <div className="card card-pad" data-idea-actions={status}>
      <span className="section-h" style={{ marginBottom: 6 }}>{tr('下一步', 'Next')}</span>
      <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.55, marginBottom: 14 }}>{hint}</div>
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        {picked ? (
          <>
            <button className="btn btn-primary" style={full} disabled={busy} onClick={onStartExperiment}>
              <Icon name="flask" size={14} />
              {tr('开始实验', 'Start experiment')}
            </button>
            <button className="btn btn-ghost" style={full} disabled={busy} onClick={onUnpick}>
              {tr('取消选中', 'Unpick')}
            </button>
          </>
        ) : rejected ? (
          <button className="btn btn-ghost" style={full} disabled={busy} onClick={onRestore}>
            {tr('恢复为候选', 'Restore as candidate')}
          </button>
        ) : (
          <>
            <button className="btn btn-primary" style={full} disabled={busy} onClick={onPick}>
              <Icon name="check" size={14} />
              {tr('选中', 'Pick')}
            </button>
            <button className="btn btn-ghost" style={full} disabled={busy} onClick={onReject}>
              <Icon name="x" size={14} />
              {tr('淘汰', 'Reject')}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
