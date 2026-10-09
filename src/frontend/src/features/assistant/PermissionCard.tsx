import { useEffect, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { ApiError, api } from '../../lib/api';
import type {
  PermissionBlock,
  PermissionOption,
  PermissionOptionKind,
  PermissionState,
} from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';
import { errorText as genericErrorText } from '../../lib/errors';
import { agentToolIcon, agentToolKind, agentToolLabel } from './agentTools';

/* ============================================================
   外部 agent 的授权请求（#838）：它要改文件、跑命令，停在这儿等你点头。

   卡片挡着 agent 往下走，所以画得比工具卡显眼；定下来之后缩成一行。
   这一帧不落库——重载历史时卡片就不在了，只剩工具卡上的「Not approved.」。
   ============================================================ */

export type PermissionStateChange = (
  requestId: string,
  state: PermissionState,
  opts?: { expired?: boolean; from?: PermissionState },
) => void;

const OPTION_LABELS: Record<PermissionOptionKind, { zh: string; en: string }> = {
  allow_once: { zh: '允许这一次', en: 'Allow once' },
  allow_always: { zh: '总是允许', en: 'Always allow' },
  reject_once: { zh: '拒绝', en: 'Deny' },
  reject_always: { zh: '总是拒绝', en: 'Always deny' },
};

/** 按钮上的字：agent 给了名字就用它的，没有就按类别给一句。 */
export function permissionOptionLabel(option: PermissionOption): string {
  const name = option.name.trim();
  if (name) return name;
  const meta = OPTION_LABELS[option.kind];
  return tr(meta.zh, meta.en);
}

export function isAllowOption(option: PermissionOption): boolean {
  return option.kind === 'allow_once' || option.kind === 'allow_always';
}

/** input 是 JSON 就排一下版，否则原样 */
function prettyInput(input: string): string {
  const raw = input.trim();
  if (!raw || (raw[0] !== '{' && raw[0] !== '[')) return raw;
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

/** m:ss */
function clock(secs: number): string {
  const s = Math.max(0, Math.ceil(secs));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

const PREVIEW_LINES = 4;
const PREVIEW_CHARS = 240;

export function PermissionCard({
  block,
  conversationId,
  turnLive,
  onStateChange,
}: {
  block: PermissionBlock;
  conversationId: string | null;
  /** 这一轮还在跑。没在跑了还挂着的请求已经没人等了，不再给按钮 */
  turnLive: boolean;
  onStateChange: PermissionStateChange;
}) {
  const [picked, setPicked] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const open = block.state === 'pending' || block.state === 'answering';
  const now = useNow(open && turnLive);

  const kind = agentToolKind(`agent_${block.toolKind}`) ?? 'other';
  const kindLabel = agentToolLabel(kind);
  const title = block.title.trim() || kindLabel;

  if (!open || !turnLive) {
    // 定下来了（或这一轮已经结束）：一行交代结果
    const allowed = block.state === 'allowed';
    const note = !open
      ? block.expired
        ? tr('（已超时）', ' (timed out)')
        : ''
      : tr('（对话已停止）', ' (reply stopped)');
    return (
      <div
        className="row gap8"
        style={{
          alignItems: 'center',
          border: '0.5px solid var(--border-2)',
          borderRadius: 8,
          background: 'var(--surface-2)',
          padding: '6px 10px',
          margin: '6px 0',
          fontSize: 12,
        }}
      >
        <Icon
          name={allowed ? 'check' : 'x'}
          size={12}
          style={{ color: allowed ? 'var(--ok-tx)' : 'var(--danger-tx)', flexShrink: 0 }}
        />
        <span style={{ color: allowed ? 'var(--ok-tx)' : 'var(--danger-tx)', flexShrink: 0 }}>
          {open ? tr('没有回答', 'Not answered') : allowed ? tr('已允许', 'Allowed') : tr('已拒绝', 'Denied')}
        </span>
        <span
          className="mono"
          title={title}
          style={{ color: 'var(--text-3)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        >
          {title}
        </span>
        {note && <span style={{ color: 'var(--text-3)', flexShrink: 0 }}>{note}</span>}
      </div>
    );
  }

  const preview = prettyInput(block.input);
  const long = preview.split('\n').length > PREVIEW_LINES || preview.length > PREVIEW_CHARS;
  const remaining = block.timeoutS - (now - block.receivedAt) / 1000;
  const answering = block.state === 'answering';

  const answer = async (option: PermissionOption) => {
    if (answering || !conversationId) return;
    setPicked(option.id);
    onStateChange(block.requestId, 'answering', { from: 'pending' });
    try {
      await api.answerAgentPermission(conversationId, block.requestId, option.id);
      // 流里随后会来 permission_resolved；先按选的那一项落定，别让人干等
      onStateChange(block.requestId, isAllowOption(option) ? 'allowed' : 'denied', { from: 'answering' });
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        onStateChange(block.requestId, 'denied', { expired: true, from: 'answering' });
        return;
      }
      setPicked(null);
      toast(`${tr('没能提交，请重试：', 'Couldn’t send your answer. Try again: ')}${genericErrorText(e)}`, 'error');
      onStateChange(block.requestId, 'pending', { from: 'answering' });
    }
  };

  return (
    <div
      role="group"
      aria-label={tr('需要你批准', 'Needs your approval')}
      style={{
        border: '1px solid var(--warn)',
        borderRadius: 9,
        background: 'var(--warn-bg)',
        padding: '9px 11px',
        margin: '8px 0',
      }}
    >
      <div className="row gap6" style={{ alignItems: 'center', fontSize: 12, color: 'var(--warn-tx)', fontWeight: 600 }}>
        <Icon name="shield" size={12} />
        <span>{tr('需要你批准', 'Needs your approval')}</span>
      </div>
      <div className="row gap6" style={{ alignItems: 'center', marginTop: 6, fontSize: 13, minWidth: 0 }}>
        <span className="row gap4" style={{ color: 'var(--text-2)', alignItems: 'center', flexShrink: 0 }}>
          <Icon name={agentToolIcon(kind)} size={12} />
          {kindLabel}
        </span>
        <span
          className="mono"
          title={title}
          style={{ color: 'var(--text)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        >
          {title}
        </span>
      </div>
      {preview && (
        <div style={{ marginTop: 6 }}>
          <pre
            className="mono"
            style={{
              margin: 0,
              maxHeight: expanded ? 260 : `${PREVIEW_LINES * 1.5}em`,
              overflow: expanded ? 'auto' : 'hidden',
              fontSize: 11,
              lineHeight: 1.5,
              color: 'var(--text-2)',
              background: 'var(--surface)',
              border: '0.5px solid var(--border-2)',
              borderRadius: 6,
              padding: '5px 8px',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
            }}
          >
            {preview}
          </pre>
          {long && (
            <button
              className="btn btn-ghost sm"
              style={{ height: 22, fontSize: 11, marginTop: 2 }}
              aria-expanded={expanded}
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? tr('收起', 'Show less') : tr('展开全部', 'Show all')}
            </button>
          )}
        </div>
      )}
      <div className="row gap6" style={{ marginTop: 9, flexWrap: 'wrap' }}>
        {block.options.map((option) => {
          const allow = isAllowOption(option);
          const cls = allow
            ? option.kind === 'allow_once'
              ? 'btn btn-primary sm'
              : 'btn btn-soft sm'
            : 'btn btn-ghost sm';
          return (
            <button
              key={option.id}
              type="button"
              className={cls}
              disabled={answering || !conversationId}
              onClick={() => void answer(option)}
            >
              {answering && picked === option.id ? tr('正在提交…', 'Sending…') : permissionOptionLabel(option)}
            </button>
          );
        })}
      </div>
      <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-3)' }}>
        {block.timeoutS >= 60
          ? tr(
              `${Math.round(block.timeoutS / 60)} 分钟内未回答将自动拒绝`,
              `Denied automatically after ${Math.round(block.timeoutS / 60)} min without an answer`,
            )
          : tr(
              `${Math.round(block.timeoutS)} 秒内未回答将自动拒绝`,
              `Denied automatically after ${Math.round(block.timeoutS)} s without an answer`,
            )}
        {remaining > 0 && (
          <span className="mono" style={{ marginLeft: 6, color: 'var(--text-3)' }}>
            {tr(`还剩 ${clock(remaining)}`, `${clock(remaining)} left`)}
          </span>
        )}
      </div>
    </div>
  );
}
