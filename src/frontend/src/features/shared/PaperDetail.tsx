import type { ReactNode } from 'react';
import { plainSummary } from '../../lib/format';
import { tr } from '../../lib/i18n';
import './papers.css';

/* ============================================================
   论文详情的公共骨架。五处详情面板（文献库 / 我的文献库 / 每日新论文 /
   相关研究 / 阅读页的信息栏）都用这几块，顺序也一样：

     出处一行（年份 · 期刊 · 编号）
     标题、作者
     动作（一个主动作：读论文）
     事实行（为什么在这里 / PDF 与全文 / 我的状态）
     要点（TL;DR，只出现一次）
     小节：解读 / 摘要 / 笔记 / 更多
   ============================================================ */

/** 出处一行：年份 · 期刊/会议 · arXiv / DOI 链接。 */
export function PaperKicker({
  year,
  venue,
  arxivId,
  doi,
  url,
  extra,
}: {
  year?: number | string | null;
  venue?: string | null;
  arxivId?: string | null;
  doi?: string | null;
  url?: string | null;
  extra?: ReactNode;
}) {
  const parts: ReactNode[] = [];
  if (year) parts.push(<span key="y">{year}</span>);
  if (venue) parts.push(<span key="v">{venue}</span>);
  if (arxivId) {
    parts.push(
      <a key="a" href={`https://arxiv.org/abs/${arxivId}`} target="_blank" rel="noreferrer noopener">
        arXiv {arxivId}
      </a>,
    );
  } else if (doi) {
    parts.push(
      <a key="d" href={`https://doi.org/${doi}`} target="_blank" rel="noreferrer noopener">
        DOI {doi}
      </a>,
    );
  } else if (url) {
    parts.push(
      <a key="u" href={url} target="_blank" rel="noreferrer noopener">
        {tr('原文链接', 'Source page')}
      </a>,
    );
  }
  if (extra) parts.push(<span key="x">{extra}</span>);
  if (parts.length === 0) return null;
  return (
    <div className="pd-kicker">
      {parts.map((p, i) => (
        <span key={i} style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
          {i > 0 && <span aria-hidden>·</span>}
          {p}
        </span>
      ))}
    </div>
  );
}

/** 事实行的容器与一行。 */
export function PaperFacts({ children }: { children: ReactNode }) {
  return <div className="pd-facts">{children}</div>;
}
export function PaperFact({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div className="pd-fact" data-testid={testId}>
      <span className="pd-fact-label">{label}</span>
      <div className="pd-fact-body">{children}</div>
    </div>
  );
}

/** 要点：一句话解读，放在所有长内容前面。children 给了就用它（可带出处链接），否则用纯文本。 */
export function PaperKeyPoints({ tldr, children }: { tldr?: string | null; children?: ReactNode }) {
  const text = plainSummary(tldr);
  if (!children && !text) return null;
  return (
    <div className="pd-key" data-testid="paper-keypoints">
      <div className="pd-key-label">{tr('要点', 'Key points')}</div>
      <div className="pd-key-body">{children ?? text}</div>
    </div>
  );
}

export interface PaperDetailTab<K extends string> {
  key: K;
  label: string;
  count?: number;
}
export function PaperDetailTabs<K extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: PaperDetailTab<K>[];
  value: K;
  onChange: (k: K) => void;
}) {
  return (
    <div className="pd-tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.key}
          type="button"
          role="tab"
          aria-selected={t.key === value}
          className={'pd-tab' + (t.key === value ? ' is-active' : '')}
          data-testid={`pd-tab-${t.key}`}
          onClick={() => onChange(t.key)}
        >
          {t.label}
          {t.count !== undefined && t.count > 0 && <span className="pd-tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

/**
 * 解读正文的开头常常就是一段「## TL;DR」——和上面的要点是同一段话。
 * 把它从正文里拿出来：lead 是那一段（带出处标记的原文），body 是剩下的正文。
 * 正文不是以这样的小节开头时 lead 为空，body 原样返回。
 */
export function splitSummary(content: string | null | undefined): { lead: string; body: string } {
  const src = (content ?? '').replace(/^﻿/, '');
  const m = src.match(/^\s*#{1,4}\s*(?:TL\s*;?\s*DR|一句话(?:总结|概括|解读)?|核心要点|要点|摘要速览)\s*[:：]?\s*\n+/i);
  if (!m) return { lead: '', body: src };
  const rest = src.slice(m[0].length);
  const next = rest.search(/^#{1,4}\s+\S/m);
  if (next < 0) return { lead: rest.trim(), body: '' };
  return { lead: rest.slice(0, next).trim(), body: rest.slice(next) };
}
