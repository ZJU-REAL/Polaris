import type { ReadingStatus } from '../../lib/api';

/* ============================================================
   阅读工作台共享：阅读状态元信息（文案 + 配色）。
   PapersTab 的列表小圆点 / 过滤器也复用这里。
   ============================================================ */

export interface ReadingStatusMeta {
  v: ReadingStatus;
  label: string;
  /** 前景色 token */
  c: string;
  /** 背景色 token */
  bg: string;
}

export const READING_STATUS: readonly ReadingStatusMeta[] = [
  { v: 'unread', label: '未读', c: 'var(--text-3)', bg: 'var(--surface-3)' },
  { v: 'reading', label: '在读', c: 'var(--warn-tx)', bg: 'var(--warn-bg)' },
  { v: 'read', label: '已读', c: 'var(--ok-tx)', bg: 'var(--ok-bg)' },
] as const;

export function readingStatusMeta(s: string | undefined): ReadingStatusMeta {
  return READING_STATUS.find((m) => m.v === s) ?? READING_STATUS[0]!;
}

/** 列表行里的阅读状态小圆点（未读不显示）。 */
export function ReadingDot({ status }: { status: string | undefined }) {
  if (!status || status === 'unread') return null;
  const meta = readingStatusMeta(status);
  return (
    <span
      title={`阅读状态：${meta.label}`}
      style={{
        width: 7,
        height: 7,
        borderRadius: '50%',
        background: meta.c,
        flexShrink: 0,
        display: 'inline-block',
      }}
    />
  );
}
