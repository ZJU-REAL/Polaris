import type { HighlightColor, ReadingStatus } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   阅读工作台共享：阅读状态元信息（文案 + 配色）。
   PapersTab 的列表小圆点 / 过滤器也复用这里。
   ============================================================ */

export interface ReadingStatusMeta {
  v: ReadingStatus;
  /** 中文文案（模块级常量，渲染处用 tr(label, en) 取当前语言） */
  label: string;
  /** 英文文案 */
  en: string;
  /** 前景色 token */
  c: string;
  /** 背景色 token */
  bg: string;
}

export const READING_STATUS: readonly ReadingStatusMeta[] = [
  { v: 'unread', label: '未读', en: 'Unread', c: 'var(--text-3)', bg: 'var(--surface-3)' },
  { v: 'reading', label: '在读', en: 'Reading', c: 'var(--warn-tx)', bg: 'var(--warn-bg)' },
  { v: 'read', label: '已读', en: 'Read', c: 'var(--ok-tx)', bg: 'var(--ok-bg)' },
] as const;

export function readingStatusMeta(s: string | undefined): ReadingStatusMeta {
  return READING_STATUS.find((m) => m.v === s) ?? READING_STATUS[0]!;
}

/* ============================================================
   划线标注配色：一处定义，PdfReader 的色块 / HighlightsPanel 的
   色点与卡片描边都复用。solid 用于色点/描边，wash 是叠在 PDF 上的
   半透明色块（配 mix-blend-mode: multiply 达到荧光笔效果）。
   ============================================================ */

export interface HighlightColorMeta {
  v: HighlightColor;
  label: string;
  solid: string;
  wash: string;
}

export const HIGHLIGHT_COLORS: readonly HighlightColorMeta[] = [
  { v: 'yellow', label: '黄', solid: '#f5c518', wash: 'rgba(250, 204, 21, 0.45)' },
  { v: 'green', label: '绿', solid: '#22c55e', wash: 'rgba(34, 197, 94, 0.40)' },
  { v: 'blue', label: '蓝', solid: '#3b82f6', wash: 'rgba(59, 130, 246, 0.35)' },
  { v: 'pink', label: '粉', solid: '#ec4899', wash: 'rgba(236, 72, 153, 0.35)' },
  { v: 'purple', label: '紫', solid: '#a855f7', wash: 'rgba(168, 85, 247, 0.35)' },
] as const;

export function highlightColorMeta(c: string | undefined): HighlightColorMeta {
  return HIGHLIGHT_COLORS.find((m) => m.v === c) ?? HIGHLIGHT_COLORS[0]!;
}

/** 列表行里的阅读状态小圆点（未读不显示）。 */
export function ReadingDot({ status }: { status: string | undefined }) {
  if (!status || status === 'unread') return null;
  const meta = readingStatusMeta(status);
  return (
    <span
      title={tr(`阅读状态：${meta.label}`, `Reading status: ${meta.en}`)}
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
