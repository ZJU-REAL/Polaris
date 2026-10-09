import { Icon } from '../../../components/ui/Icon';
import { tr } from '../../../lib/i18n';
import { enCount } from './stepUtils';
import type { VoyageDetail, VoyageStatus, VoyageStepRead } from '../../../lib/api';

/* ============================================================
   顶部活动状态区：执行方式徽标 + 当前活动一句话 + 暂停态操作条。
   从 VoyageDetailPage 抽出，供任务详情页与实验运行台共用。
   ============================================================ */

/** mode → 大白话标签与说明（模块级常量只存 zh/en 字段，渲染处再 tr）。 */
export const MODE_INFO: Record<string, { zh: string; en: string; hintZh: string; hintEn: string }> = {
  pipeline: {
    zh: '固定步骤',
    en: 'Fixed steps',
    hintZh: '按创建时定好的步骤依次执行',
    hintEn: 'Runs the steps set at creation, in order',
  },
  template: {
    zh: '按模板',
    en: 'Template',
    hintZh: '按模板执行，必要时根据结果补充步骤',
    hintEn: 'Follows a template and adds steps when results call for it',
  },
  loop: {
    zh: 'AI 规划',
    en: 'AI-planned',
    hintZh: '每步完成后检查结果，再调整后续步骤',
    hintEn: 'Checks each step’s result, then adjusts the remaining steps',
  },
};

export function ModeBadge({ mode }: { mode: string }) {
  const m = MODE_INFO[mode];
  if (!m) return null;
  return (
    <span
      className="pill sm"
      style={{ background: 'var(--surface-3)', color: 'var(--text-2)', flexShrink: 0 }}
      title={tr(m.hintZh, m.hintEn)}
    >
      <Icon name={mode === 'loop' ? 'sparkle' : 'layers'} size={11} />
      {tr(m.zh, m.en)}
    </span>
  );
}

/** 从 status + cursor + steps 推导当前活动的一句话。 */
export function activityText(voyage: VoyageDetail, steps: VoyageStepRead[]): string {
  const live = steps.filter((s) => s.status !== 'obsolete');
  let curIdx = live.findIndex((s) => s.status === 'running' || s.status === 'verifying');
  if (curIdx < 0 && typeof voyage.cursor === 'number' && voyage.cursor >= 0 && voyage.cursor < live.length) {
    curIdx = voyage.cursor;
  }
  const cur = curIdx >= 0 ? live[curIdx] : null;
  const stepRef = cur
    ? tr(`第 ${curIdx + 1} 步 · ${cur.title}`, `step ${curIdx + 1} · ${cur.title}`)
    : null;

  switch (voyage.status) {
    case 'planning':
      return tr('正在规划步骤…', 'Planning steps…');
    case 'executing': {
      if (!stepRef) return tr('正在执行…', 'Running…');
      const runSuffix =
        cur && cur.attempt > 1
          ? tr(`（第 ${cur.attempt} 次尝试）`, ` (attempt ${cur.attempt})`)
          : '';
      return tr(`正在执行：${stepRef}${runSuffix}`, `Running ${stepRef}${runSuffix}`);
    }
    case 'verifying':
      return stepRef
        ? tr(`正在校验：${stepRef}`, `Checking ${stepRef}`)
        : tr('正在校验结果…', 'Checking results…');
    case 'replanning':
      return tr('正在调整计划…', 'Adjusting the plan…');
    case 'paused_gate':
      return tr('已暂停：等待审批', 'Paused: waiting for approval');
    case 'paused_error':
      return tr('已暂停：执行出错', 'Paused after an error');
    case 'paused_ask':
      return tr('已暂停：AI 有问题要问你', 'Paused: the AI has a question');
    case 'done': {
      const passed = live.filter((s) => s.status === 'passed').length;
      const adj = voyage.plan_iteration ?? 0;
      return (
        tr(`已完成，共 ${passed} 步`, `Done: ${enCount(passed, 'step')}`) +
        (adj > 0 ? tr(`，调整计划 ${adj} 次`, `, plan adjusted ${enCount(adj, 'time')}`) : '')
      );
    }
    case 'failed':
      return tr('任务失败', 'Task failed');
    case 'cancelled':
      return tr('任务已取消', 'Task cancelled');
  }
}

export function activityDot(status: VoyageStatus): { color: string; pulse: boolean } {
  switch (status) {
    case 'paused_gate':
    case 'paused_ask':
      return { color: 'var(--warn-tx)', pulse: false };
    case 'paused_error':
    case 'failed':
      return { color: 'var(--danger-tx)', pulse: false };
    case 'done':
      return { color: 'var(--ok)', pulse: false };
    case 'cancelled':
      return { color: 'var(--text-4)', pulse: false };
    default:
      return { color: 'var(--accent)', pulse: true };
  }
}

export function ActivityBar({
  voyage,
  steps,
  onOpenGates,
  onResume,
  resuming,
  onReply,
}: {
  voyage: VoyageDetail;
  steps: VoyageStepRead[];
  onOpenGates: () => void;
  onResume?: () => void;
  resuming?: boolean;
  /** paused_ask 时「去回复」按钮的回调（滚动/聚焦到对话输入框） */
  onReply?: () => void;
}) {
  const status = voyage.status;
  const dot = activityDot(status);
  const live = steps.filter((s) => s.status !== 'obsolete');
  const passed = live.filter((s) => s.status === 'passed').length;
  return (
    <div>
      <div className="row gap10" style={{ flexWrap: 'wrap' }}>
        <span className={'dot' + (dot.pulse ? ' pulse' : '')} style={{ background: dot.color, flexShrink: 0 }} />
        <span style={{ fontSize: 13, fontWeight: 600, minWidth: 0 }}>{activityText(voyage, steps)}</span>
        <div className="row gap8" style={{ marginLeft: 'auto' }}>
          {live.length > 0 && (
            <span className="mono muted" style={{ fontSize: 11 }}>
              {tr(`已完成 ${passed}/${live.length} 步`, `${passed}/${live.length} steps done`)}
            </span>
          )}
          <ModeBadge mode={voyage.mode} />
        </div>
      </div>
      {status === 'paused_gate' && (
        <div
          className="row gap8"
          style={{
            marginTop: 12,
            padding: '10px 14px',
            background: 'var(--warn-bg)',
            color: 'var(--warn-tx)',
            borderRadius: 10,
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          <Icon name="gate" size={15} />
          {tr('批准后任务会继续。', 'The task continues once you approve.')}
          <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} onClick={onOpenGates}>
            {tr('查看审批', 'Review')}
          </button>
        </div>
      )}
      {status === 'paused_ask' && (
        <div
          className="row gap8"
          style={{
            marginTop: 12,
            padding: '10px 14px',
            background: 'var(--warn-bg)',
            color: 'var(--warn-tx)',
            borderRadius: 10,
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          <Icon name="sparkle" size={15} />
          {tr('回复后任务会继续。', 'The task continues once you reply.')}
          {onReply && (
            <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} onClick={onReply}>
              {tr('回复', 'Reply')}
            </button>
          )}
        </div>
      )}
      {status === 'paused_error' && (
        <div className="row gap8" style={{ marginTop: 12, padding: '10px 14px', background: 'var(--danger-bg)', color: 'var(--danger-tx)', borderRadius: 10, fontSize: 13 }}>
          <Icon name="x" size={14} />
          {tr('重试会从出错的步骤继续，已完成的步骤不重跑。', 'Retrying picks up at the failed step. Finished steps don’t rerun.')}
          {onResume && (
            <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} disabled={resuming} onClick={onResume}>
              {resuming ? tr('重试中…', 'Retrying…') : tr('重试', 'Retry')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
