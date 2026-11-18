import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { tr } from '../../lib/i18n';
import { MODEL_SETTINGS_HREF } from '../../lib/llmNotConfigured';
import type { ResearchAttention, ResearchLastError, ResearchRunDetail, RetryRunInput } from '../../lib/researchApi';
import { StatusDot } from '../voyages/shared/StatusDot';
import { useNow } from './hooks';
import {
  attentionActionLabel,
  attentionActions,
  attentionText,
  retryText,
  sectionStateLabel,
  type AttentionAction,
  type SectionState,
} from './phase';

/* ============================================================
   时间线各段共用的小件：段的外壳、卡住时的说明 + 动作、自动重试的说明、进度条。
   ============================================================ */

/** 各段都要用到的页面级动作，由 ResearchRunPage 提供。 */
export interface RunActions {
  /** 重试当前这一步（可带新范围 / 放宽范围） */
  retry: (input?: RetryRunInput) => void;
  /** 打开范围编辑（focus：光标放在关键词上） */
  editScope: () => void;
  /** 有动作正在提交：按钮置灰，避免连点 */
  busy: boolean;
  /** 范围编辑器开着：主按钮在编辑器里，别处的主动作让位 */
  editing: boolean;
}

export interface SectionProps {
  title: string;
  state: SectionState;
  /** 覆盖默认的状态词（如等用户时说清是「等你回答」还是「等你选定」） */
  stateLabel?: string;
  /** 标题行右侧的安静链接（如「详细日志」） */
  aside?: ReactNode;
  /** 最后一段不画连接线 */
  last?: boolean;
  children?: ReactNode;
}

/** 时间线的一段：左边一条细轨道 + 状态标记，右边是小标题、状态词和内容。 */
export function Section({ title, state, stateLabel, aside, last, children }: SectionProps) {
  const label = sectionStateLabel(state);
  return (
    <section className={`dr-sec is-${state}` + (last ? ' is-last' : '')}>
      <div className="dr-sec-rail" aria-hidden="true">
        <span className="dr-sec-mark">{state === 'done' && <Icon name="check" size={10} sw={2.4} />}</span>
        {!last && <span className="dr-sec-line" />}
      </div>
      <div className="dr-sec-body">
        <div className="dr-sec-head">
          <h2 className="dr-sec-title">{title}</h2>
          {label && <StatusDot tone={label.tone} label={stateLabel ?? label.label} />}
          {aside && <div className="dr-sec-aside">{aside}</div>}
        </div>
        {children}
      </div>
    </section>
  );
}

/** 还没轮到的一段：一句话说这里之后会发生什么。 */
export function PendingLine({ children }: { children: ReactNode }) {
  return <p className="dr-pending">{children}</p>;
}

/** 进度条：有比例画比例，没有就画来回移动的不定进度。 */
export function ProgressBar({ fraction, label }: { fraction: number | null; label: string }) {
  const pct = fraction === null ? null : Math.round(Math.max(0, Math.min(1, fraction)) * 100);
  return (
    <div
      className={'dr-bar' + (pct === null ? ' is-indeterminate' : '')}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct ?? undefined}
    >
      <span style={pct === null ? undefined : { width: `${pct}%` }} />
    </div>
  );
}

/** 卡住了：一句原因 + 一个主动作（个别原因多一个次要动作）。 */
export function AttentionBlock({
  attention,
  run,
  actions,
}: {
  attention: ResearchAttention;
  run: ResearchRunDetail;
  actions: RunActions;
}) {
  const { primary, secondary } = attentionActions(attention.code);
  const libraryHref = run.library_id ? `/libraries/${run.library_id}` : '/libraries';

  function button(action: AttentionAction, isPrimary: boolean) {
    // 范围编辑器开着时，这一屏的主按钮是编辑器里的「重新检索」
    const cls = 'btn sm ' + (isPrimary && !actions.editing ? 'btn-primary' : isPrimary ? 'btn-ghost' : 'btn-plain');
    const label = attentionActionLabel(action);
    switch (action) {
      case 'add_papers':
        return (
          <Link key={action} className={cls} to={libraryHref}>
            {label}
          </Link>
        );
      case 'settings':
        return (
          <Link key={action} className={cls} to={MODEL_SETTINGS_HREF}>
            {label}
          </Link>
        );
      case 'edit_scope':
        if (actions.editing) return null;
        return (
          <button key={action} type="button" className={cls} disabled={actions.busy} onClick={actions.editScope}>
            {label}
          </button>
        );
      case 'broaden':
        return (
          <button key={action} type="button" className={cls} disabled={actions.busy} onClick={() => actions.retry({ broaden: true })}>
            {label}
          </button>
        );
      default:
        return (
          <button key={action} type="button" className={cls} disabled={actions.busy} onClick={() => actions.retry()}>
            {label}
          </button>
        );
    }
  }

  return (
    <div className="dr-attn" role="alert">
      <div className="dr-attn-text">{attentionText(attention)}</div>
      {attention.detail && <div className="dr-attn-detail">{attention.detail}</div>}
      <div className="dr-attn-actions">
        {button(primary, true)}
        {secondary && button(secondary, false)}
      </div>
    </div>
  );
}

/** 遇到临时问题、排了自动重试：说清楚出了什么事、多久后重试、第几次；不想等可以现在重试。 */
export function RetryBlock({
  nextRetryAt,
  attempt,
  lastError,
  actions,
}: {
  nextRetryAt: string;
  attempt: number | null | undefined;
  lastError: ResearchLastError | null | undefined;
  actions: RunActions;
}) {
  const now = useNow(15_000);
  const at = new Date(nextRetryAt);
  const clock = Number.isNaN(at.getTime())
    ? ''
    : `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`;
  return (
    <div className="dr-attn is-quiet" role="status">
      <div className="dr-attn-text">
        {lastError?.message
          ? tr(`遇到临时问题：${lastError.message}`, `Hit a temporary problem: ${lastError.message}`)
          : tr('遇到临时问题', 'Hit a temporary problem')}
      </div>
      <div className="dr-attn-sub" title={clock ? tr(`${clock} 重试`, `Retries at ${clock}`) : undefined}>
        {retryText(nextRetryAt, attempt, now)}
        {tr('，不需要你操作。', '. Nothing for you to do.')}
      </div>
      <div className="dr-attn-actions">
        <button type="button" className="btn btn-ghost sm" disabled={actions.busy} onClick={() => actions.retry()}>
          {tr('现在重试', 'Retry now')}
        </button>
      </div>
    </div>
  );
}

/** 用户停在这一段：说明东西还在，给一个继续的按钮。 */
export function StoppedBlock({ text, actions }: { text: string; actions: RunActions }) {
  return (
    <div className="dr-attn is-quiet">
      <div className="dr-attn-text">{text}</div>
      <div className="dr-attn-actions">
        <button
          type="button"
          className={'btn sm ' + (actions.editing ? 'btn-ghost' : 'btn-primary')}
          disabled={actions.busy}
          onClick={() => actions.retry()}
        >
          {tr('继续研究', 'Resume research')}
        </button>
      </div>
    </div>
  );
}

/** 正在做的事 + 一句对等待的交代。 */
export function NowDoing({ caption, wait }: { caption: string | null; wait: string }) {
  return (
    <div className="dr-now">
      {caption && (
        <div className="dr-now-caption" title={caption}>
          {caption}
        </div>
      )}
      <div className="dr-now-wait">{wait}</div>
    </div>
  );
}
