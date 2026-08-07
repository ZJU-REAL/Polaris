import { Icon } from '../../../components/ui/Icon';
import { tr } from '../../../lib/i18n';
import type { VoyageDetail, VoyageStatus, VoyageStepRead } from '../../../lib/api';

/* ============================================================
   顶部活动状态区：执行方式徽标 + 当前活动一句话 + 暂停态操作条。
   从 VoyageDetailPage 抽出，供任务详情页与实验运行台共用。
   ============================================================ */

/** mode → 大白话标签与说明（模块级常量只存 zh/en 字段，渲染处再 tr）。 */
export const MODE_INFO: Record<string, { zh: string; en: string; hintZh: string; hintEn: string }> = {
  pipeline: {
    zh: '固定流程',
    en: 'Fixed pipeline',
    hintZh: '步骤在创建时已完全确定，按固定顺序执行，不会中途调整计划',
    hintEn: 'Steps are fully fixed at creation and run in order; the plan never changes mid-run',
  },
  template: {
    zh: '模板流程',
    en: 'Template flow',
    hintZh: '按预设模板执行，在预设的分支点根据执行结果补充后续步骤',
    hintEn: 'Runs from a preset template; follow-up steps are added at preset branch points based on results',
  },
  loop: {
    zh: 'AI 动态规划',
    en: 'AI dynamic planning',
    hintZh: '循环推进：每步执行后自动校验，再按结果动态调整后续计划（规则分支优先，AI 兜底）',
    hintEn: 'Runs in a loop: each step is auto-checked, then the remaining plan is adjusted based on results (preset rules first, AI as fallback)',
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
      return tr('AI 正在规划步骤…', 'AI is planning the steps…');
    case 'executing': {
      if (!stepRef) return tr('正在执行步骤…', 'Executing steps…');
      const runSuffix =
        cur && cur.attempt > 1
          ? tr(`（第 ${cur.attempt} 次运行）`, ` (run ${cur.attempt})`)
          : '';
      return tr(`正在执行：${stepRef}${runSuffix}`, `Executing ${stepRef}${runSuffix}`);
    }
    case 'verifying':
      return stepRef
        ? tr(`正在校验：${stepRef}`, `Checking ${stepRef}`)
        : tr('正在校验执行结果…', 'Checking results…');
    case 'replanning':
      return tr(
        `正在调整计划（第 ${(voyage.plan_iteration ?? 0) + 1} 次）…`,
        `Adjusting the plan (adjustment ${(voyage.plan_iteration ?? 0) + 1})…`,
      );
    case 'paused_gate':
      return tr('已暂停：等待人工审批', 'Paused: waiting for approval');
    case 'paused_error':
      return tr('已暂停：执行出错', 'Paused: an error occurred');
    case 'paused_ask':
      return tr('已暂停：AI 有问题想问你', 'Paused: the AI has a question for you');
    case 'done': {
      const passed = live.filter((s) => s.status === 'passed').length;
      const adj = voyage.plan_iteration ?? 0;
      return (
        tr(`任务完成：共执行 ${passed} 步`, `Task finished: ${passed} steps completed`) +
        (adj > 0 ? tr(`，期间计划调整 ${adj} 次`, `; the plan was adjusted ${adj} time(s)`) : '')
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
        <span style={{ fontSize: 13.5, fontWeight: 650, minWidth: 0 }}>{activityText(voyage, steps)}</span>
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
            fontSize: 12.5,
            fontWeight: 600,
          }}
        >
          <Icon name="gate" size={15} />
          {tr('任务已暂停，等待人工审批后继续。', 'Task paused — it will continue after approval.')}
          <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} onClick={onOpenGates}>
            {tr('前往审批', 'Go to approvals')}
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
            fontSize: 12.5,
            fontWeight: 600,
          }}
        >
          <Icon name="sparkle" size={15} />
          {tr('AI 有问题想问你，回复后任务会继续。', 'The AI has a question — reply to continue the task.')}
          {onReply && (
            <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} onClick={onReply}>
              {tr('去回复', 'Reply')}
            </button>
          )}
        </div>
      )}
      {status === 'paused_error' && (
        <div className="row gap8" style={{ marginTop: 12, padding: '10px 14px', background: 'var(--danger-bg)', color: 'var(--danger-tx)', borderRadius: 10, fontSize: 12.5 }}>
          <Icon name="x" size={14} />
          {tr('任务因错误暂停。重试会从断点继续，已完成的步骤不会重跑。', 'Task paused on an error. Retrying resumes from where it stopped — finished steps will not rerun.')}
          {onResume && (
            <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} disabled={resuming} onClick={onResume}>
              {resuming ? tr('重试中…', 'Retrying…') : tr('重试恢复', 'Retry & resume')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
