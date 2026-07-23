import type { HighlightColor, HighlightStyle, ReadingStatus } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   进阅读页时携带的「上游来源」：/papers/:id/read 的返回按钮
   据此回到用户真正来的那个界面（相关研究 / 我的文献库 / 文献库），
   而不是一律回论文所属课题的方向库。刷新丢失 state 时退回默认。
   ============================================================ */

export type ReaderFromKind = 'research' | 'library' | 'wiki';

export interface ReaderFrom {
  /** 返回目标的完整 in-app 路径（pathname + search）。 */
  href: string;
  kind: ReaderFromKind;
}

/** 由当前 location 构造进阅读页用的 navigate state。 */
export function readerFrom(loc: { pathname: string; search: string }, kind: ReaderFromKind): { from: ReaderFrom } {
  return { from: { href: loc.pathname + loc.search, kind } };
}

/** 返回按钮文案：按来源界面给出对应的「回…」。 */
export function readerBackLabel(kind: ReaderFromKind): string {
  switch (kind) {
    case 'research':
      return tr('回相关研究', 'Back to related work');
    case 'library':
      return tr('回我的文献库', 'Back to my library');
    default:
      return tr('回文献库', 'Back to library');
  }
}

/* ============================================================
   阅读工作台共享：阅读状态元信息（文案 + 配色）。
   PapersTab 的列表小圆点 / 过滤器也复用这里。
   ============================================================ */

export interface ReadingStatusMeta {
  v: ReadingStatus;
  label: string;
  /** 英文副标题（tr(label, en) 单语切换用） */
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

/* 标注样式：高亮块 / 下方横线 / 下方波浪线。 */
export interface HighlightStyleMeta {
  v: HighlightStyle;
  label: string;
}

export const HIGHLIGHT_STYLES: readonly HighlightStyleMeta[] = [
  { v: 'highlight', label: '高亮' },
  { v: 'underline', label: '下划线' },
  { v: 'wave', label: '波浪线' },
] as const;

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
