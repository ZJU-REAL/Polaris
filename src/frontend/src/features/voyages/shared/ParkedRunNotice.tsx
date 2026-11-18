import { useEffect, useState, type ReactNode } from 'react';
import { Icon } from '../../../components/ui/Icon';
import { tr } from '../../../lib/i18n';
import { parkedErrorDetail, parkedErrorText, retryInfo, type ParkedError } from './parkedRun';

/* ============================================================
   出错暂停的任务的说明块：为什么停下（按错误码出文案）+ 原始报错（等宽小字）+
   自动重试的安排 + 一个「重试」主按钮。任务详情页与想法页的提醒条共用。

   以前这里只有一句固定的「重试会从出错的步骤继续」，为什么停、会不会自己再试都看不到。
   ============================================================ */

export function ParkedRunNotice({
  title,
  error,
  stepTitle,
  retryAt,
  retryAttempt,
  onRetry,
  retrying,
  extraActions,
}: {
  /** 可选的标题行（想法页用来说明是哪种任务停了；任务详情页自己有标题，不传） */
  title?: string;
  error: ParkedError | null | undefined;
  /** 出错的步骤名（引擎有记才显示） */
  stepTitle?: string | null;
  retryAt: string | null | undefined;
  retryAttempt: number | null | undefined;
  onRetry?: () => void;
  retrying?: boolean;
  /** 「重试」右边的次要操作（查看任务 / 取消） */
  extraActions?: ReactNode;
}) {
  // 「N 分钟后」要跟着时间走：排了自动重试时每 20 秒重算一次
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!retryAt) return;
    const timer = setInterval(() => setNow(Date.now()), 20_000);
    return () => clearInterval(timer);
  }, [retryAt]);

  const detail = parkedErrorDetail(error);
  const retry = retryInfo(retryAt, retryAttempt, now);
  return (
    <div
      style={{
        padding: '12px 14px',
        background: 'var(--danger-bg)',
        color: 'var(--danger-tx)',
        borderRadius: 10,
        fontSize: 13,
        lineHeight: 1.55,
      }}
    >
      <div className="row gap10" style={{ alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <Icon name="x" size={14} style={{ flexShrink: 0, marginTop: 3 }} />
        <div style={{ flex: '1 1 240px', minWidth: 0 }}>
          {title && <div style={{ fontWeight: 600, marginBottom: 2 }}>{title}</div>}
          <div style={{ fontWeight: title ? 400 : 600, overflowWrap: 'anywhere' }}>{parkedErrorText(error)}</div>
          {stepTitle && (
            <div style={{ fontSize: 12, marginTop: 2, overflowWrap: 'anywhere' }}>
              {tr(`出错的步骤：${stepTitle}`, `Failed step: ${stepTitle}`)}
            </div>
          )}
        </div>
        <div className="row gap8" style={{ flexShrink: 0, marginLeft: 'auto' }}>
          {extraActions}
          {onRetry && (
            <button className="btn btn-primary sm" disabled={retrying} onClick={onRetry}>
              <Icon name="refresh" size={12} />
              {retrying ? tr('重试中…', 'Retrying…') : tr('重试', 'Retry')}
            </button>
          )}
        </div>
      </div>
      {detail && (
        <div
          className="mono"
          style={{
            margin: '8px 0 0 24px',
            fontSize: 11,
            lineHeight: 1.5,
            opacity: 0.85,
            whiteSpace: 'pre-wrap',
            overflowWrap: 'anywhere',
            maxHeight: 96,
            overflowY: 'auto',
          }}
        >
          {detail}
        </div>
      )}
      <div style={{ margin: '8px 0 0 24px', fontSize: 12 }}>
        {retry && (
          <span style={{ fontWeight: 600 }}>
            {retry.text}
            {tr('。', '. ')}
          </span>
        )}
        {tr('重试会从出错的步骤继续，已完成的步骤不重跑。', 'Retrying picks up at the failed step. Finished steps don’t rerun.')}
      </div>
    </div>
  );
}
