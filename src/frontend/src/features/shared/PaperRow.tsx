import type { HTMLAttributes, ReactNode } from 'react';
import { Icon } from '../../components/ui/Icon';
import { plainSummary } from '../../lib/format';
import { tr } from '../../lib/i18n';
import type { PaperAuthor } from '../../lib/api';
import './papers.css';

/* ============================================================
   论文列表里的一行。文献库、我的文献库、每日新论文、相关研究共用：

     标题（最多两行）
     第一作者 等 · 年份 · 期刊/会议 · 相关度 0.95        ★ ✎2
     一句话解读（没有解读就照实说没有）

   行里只有文字：状态不用胶囊，相关度写成词，星标和笔记数是行尾的小记号。
   ============================================================ */

/** 「Lewis 等」/「Lewis et al.」；两位作者以内全写。 */
export function shortAuthors(authors: (PaperAuthor | string)[] | null | undefined): string {
  const names = (authors ?? [])
    .map((a) => (typeof a === 'string' ? a : a?.name))
    .filter((n): n is string => !!n && n.trim().length > 0);
  if (names.length === 0) return '';
  if (names.length <= 2) return names.join(tr('、', ', '));
  return tr(`${names[0]} 等`, `${names[0]} et al.`);
}

/** 相关度写成一句人话：分数 + 它意味着什么。没有评分返回空串。 */
export function relevanceWords(score: number | null | undefined): string {
  if (score === null || score === undefined || Number.isNaN(score)) return '';
  if (score >= 0.8) return tr('和这个方向高度相关', 'Highly relevant to this direction');
  if (score >= 0.6) return tr('和这个方向相关', 'Relevant to this direction');
  if (score >= 0.4) return tr('和这个方向部分相关', 'Partly relevant to this direction');
  return tr('和这个方向关系较弱', 'Only loosely related to this direction');
}

/** 行内的相关度：「相关度 0.95」。 */
export function relevanceLabel(score: number | null | undefined): string {
  if (score === null || score === undefined || Number.isNaN(score)) return '';
  return tr(`相关度 ${score.toFixed(2)}`, `Relevance ${score.toFixed(2)}`);
}

export interface PaperRowProps {
  title: string;
  /** 元信息片段，空的自动略过，用「 · 」连起来 */
  meta: (string | number | null | undefined | false)[];
  /** 一句话解读（原样传 tldr，行内会去掉出处标记） */
  summary?: string | null;
  /** 没有 summary 时写什么；不传就不占这一行 */
  summaryFallback?: string;
  /** 解读最多几行，默认 1 */
  summaryLines?: number;
  starred?: boolean;
  noteCount?: number;
  /** 行尾的其他小记号 */
  marks?: ReactNode;
  active?: boolean;
  /** 行首（多选框） */
  leading?: ReactNode;
  /** 行尾动作（加到… / 菜单） */
  trailing?: ReactNode;
  onClick?: () => void;
  /** 拖给助手等：透传到行容器 */
  rowProps?: HTMLAttributes<HTMLDivElement>;
  testId?: string;
}

export function PaperRow({
  title,
  meta,
  summary,
  summaryFallback,
  summaryLines = 1,
  starred,
  noteCount,
  marks,
  active,
  leading,
  trailing,
  onClick,
  rowProps,
  testId,
}: PaperRowProps) {
  const metaText = meta.filter((m) => m !== null && m !== undefined && m !== false && m !== '').join(' · ');
  const sum = plainSummary(summary);
  const hasMarks = !!starred || (noteCount ?? 0) > 0 || !!marks;
  return (
    <div
      {...rowProps}
      className={'pl-row' + (active ? ' is-active' : '') + (onClick ? '' : ' is-static')}
      data-testid={testId ?? 'paper-row'}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      aria-current={active ? 'true' : undefined}
      onClick={onClick}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.target !== e.currentTarget) return;
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
    >
      {leading && <span className="pl-lead">{leading}</span>}
      <div className="pl-main">
        <div className="pl-title" title={title}>
          {title}
        </div>
        {(metaText || hasMarks) && (
          <div className="pl-meta">
            <span className="pl-meta-text" title={metaText}>
              {metaText}
            </span>
            {hasMarks && (
              <span className="pl-meta-marks">
                {starred && (
                  <span className="pl-mark is-star" title={tr('已加星标', 'Starred')}>
                    <Icon name="starFill" size={11} />
                  </span>
                )}
                {(noteCount ?? 0) > 0 && (
                  <span className="pl-mark" title={tr(`${noteCount} 条笔记`, noteCount === 1 ? '1 note' : `${noteCount} notes`)}>
                    <Icon name="pen" size={10} />
                    {noteCount}
                  </span>
                )}
                {marks}
              </span>
            )}
          </div>
        )}
        {sum ? (
          <div className="pl-sum" style={summaryLines !== 1 ? ({ '--pl-sum-lines': summaryLines } as React.CSSProperties) : undefined}>
            {sum}
          </div>
        ) : summaryFallback ? (
          <div className="pl-sum is-missing">{summaryFallback}</div>
        ) : null}
      </div>
      {trailing && (
        <span className="pl-trail" onClick={(e) => e.stopPropagation()}>
          {trailing}
        </span>
      )}
    </div>
  );
}
