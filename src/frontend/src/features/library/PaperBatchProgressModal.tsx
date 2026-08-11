import { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import type { PaperBatchItemResult, PaperBatchItemStatus } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { subscribeSse } from '../../lib/sse';

interface BatchSummary {
  created: number;
  existing: number;
  invalid: number;
  failed: number;
}

const EMPTY_SUMMARY: BatchSummary = { created: 0, existing: 0, invalid: 0, failed: 0 };

function statusMeta(status: PaperBatchItemStatus): { zh: string; en: string; icon: 'check' | 'minus' | 'x'; color: string } {
  if (status === 'created') return { zh: '已添加', en: 'Added', icon: 'check', color: 'var(--ok)' };
  if (status === 'existing') return { zh: '已存在', en: 'Existing', icon: 'minus', color: 'var(--text-3)' };
  if (status === 'invalid') return { zh: '格式错误', en: 'Invalid', icon: 'x', color: 'var(--warn)' };
  return { zh: '添加失败', en: 'Failed', icon: 'x', color: 'var(--danger)' };
}

export interface PaperBatchProgressModalProps {
  taskId: string;
  total: number;
  onClose: () => void;
  onDone?: () => void;
  onOpenPaper?: (paperId: string) => void;
}

export function PaperBatchProgressModal({
  taskId,
  total,
  onClose,
  onDone,
  onOpenPaper,
}: PaperBatchProgressModalProps) {
  const [items, setItems] = useState<Record<number, PaperBatchItemResult>>({});
  const [completed, setCompleted] = useState(0);
  const [summary, setSummary] = useState<BatchSummary>(EMPTY_SUMMARY);
  const [done, setDone] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  useEffect(() => {
    let stopped = false;
    const cancel = subscribeSse(`/paper-tasks/${taskId}/events`, {
      onEvent: (event, data) => {
        if (stopped) return;
        try {
          if (event === 'batch_item') {
            const item = JSON.parse(data) as PaperBatchItemResult;
            setItems((prev) => ({ ...prev, [item.index]: item }));
          } else if (event === 'batch_enriched') {
            const payload = JSON.parse(data) as { index: number };
            setItems((prev) => {
              const item = prev[payload.index];
              return item ? { ...prev, [payload.index]: { ...item, processing: false } } : prev;
            });
          } else if (event === 'batch_progress') {
            const progress = JSON.parse(data) as BatchSummary & { completed: number };
            setCompleted(progress.completed);
            setSummary({
              created: progress.created,
              existing: progress.existing,
              invalid: progress.invalid,
              failed: progress.failed,
            });
          } else if (event === 'done') {
            const result = JSON.parse(data) as BatchSummary;
            setSummary(result);
            setCompleted(total);
            setDone(true);
            stopped = true;
            cancel();
            onDoneRef.current?.();
          } else if (event === 'error') {
            const result = JSON.parse(data) as { message?: string };
            setFatal(result.message || tr('批量任务失败', 'Batch task failed'));
            stopped = true;
            cancel();
          }
        } catch {
          /* 忽略无法解析的事件片段，后续回放仍可继续。 */
        }
      },
    });
    return () => {
      stopped = true;
      cancel();
    };
  }, [taskId, total]);

  const orderedItems = useMemo(
    () => Object.values(items).sort((left, right) => left.index - right.index),
    [items],
  );
  const percent = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  const stillProcessing = orderedItems.filter((item) => item.processing).length;

  return (
    <Modal
      open
      onClose={onClose}
      width={680}
      title={
        <>
          <Icon
            name={fatal ? 'x' : done ? 'check' : 'refresh'}
            size={15}
            style={{
              color: fatal ? 'var(--danger)' : done ? 'var(--ok)' : 'var(--accent)',
              animation: !fatal && !done ? 'spin 1s linear infinite' : undefined,
            }}
          />
          {tr('批量添加文献', 'Batch paper import')}
        </>
      }
      sub={tr(
        `${completed}/${total} 项已解析${stillProcessing ? `，${stillProcessing} 项正在后台处理` : ''}`,
        `${completed}/${total} parsed${stillProcessing ? `, ${stillProcessing} processing` : ''}`,
      )}
      footer={
        <button className="btn btn-ghost sm" onClick={onClose}>
          {done ? tr('完成', 'Done') : tr('关闭', 'Close')}
        </button>
      }
    >
      <div style={{ height: 5, borderRadius: 3, background: 'var(--surface-3)', overflow: 'hidden' }}>
        <div
          style={{
            width: `${percent}%`,
            height: '100%',
            background: fatal ? 'var(--danger)' : 'var(--accent)',
            transition: 'width 180ms ease',
          }}
        />
      </div>

      <div className="row gap12" style={{ marginTop: 12, fontSize: 11.5, color: 'var(--text-2)', flexWrap: 'wrap' }}>
        <span>{tr('已添加', 'Added')} {summary.created}</span>
        <span>{tr('已存在', 'Existing')} {summary.existing}</span>
        <span>{tr('格式错误', 'Invalid')} {summary.invalid}</span>
        <span>{tr('失败', 'Failed')} {summary.failed}</span>
      </div>

      <div className="col" style={{ marginTop: 10, maxHeight: 360, overflowY: 'auto' }}>
        {orderedItems.map((item) => {
          const meta = statusMeta(item.status);
          return (
            <div
              key={item.index}
              className="row gap10"
              style={{ minHeight: 42, padding: '7px 2px', borderBottom: '1px solid var(--border)', alignItems: 'flex-start' }}
            >
              <Icon name={meta.icon} size={13} style={{ marginTop: 2, color: meta.color, flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="row gap8" style={{ minWidth: 0 }}>
                  {item.paper_id && item.title && onOpenPaper ? (
                    <button
                      type="button"
                      className="btn btn-ghost sm"
                      style={{ minWidth: 0, padding: 0, justifyContent: 'flex-start' }}
                      onClick={() => onOpenPaper(item.paper_id as string)}
                    >
                      <span className="ellipsis">{item.title}</span>
                    </button>
                  ) : (
                    <span className="ellipsis" style={{ fontSize: 12.5 }}>
                      {item.title || item.input}
                    </span>
                  )}
                  {item.processing && (
                    <Icon name="refresh" size={11} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite', flexShrink: 0 }} />
                  )}
                </div>
                {(item.title || item.error) && (
                  <div
                    className={item.error ? undefined : 'mono'}
                    style={{
                      marginTop: 2,
                      fontSize: 10.5,
                      color: item.error ? 'var(--danger-tx)' : 'var(--text-3)',
                      lineHeight: 1.45,
                      overflowWrap: 'anywhere',
                    }}
                  >
                    {item.error || item.input}
                  </div>
                )}
              </div>
              <span style={{ color: meta.color, fontSize: 10.5, whiteSpace: 'nowrap' }}>
                {tr(meta.zh, meta.en)}
              </span>
            </div>
          );
        })}
      </div>

      {fatal && (
        <div style={{ marginTop: 12, color: 'var(--danger-tx)', background: 'var(--danger-bg)', padding: '8px 10px', borderRadius: 6 }}>
          {fatal}
        </div>
      )}
    </Modal>
  );
}
