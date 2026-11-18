import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { IdeaEvidence } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { readerHref } from '../reading/shared';
import { Markdown, type PaperRefRenderer } from '../../lib/markdown';
import { boldLeadLabels, cleanSnippet, splitBodySections, splitInlineEnumerations, type BodySection } from './ideaText';

/* ============================================================
   想法详情页里「长」的那几块：正文小节、文献依据、页内目录。

   真实模型写的想法，正文四节加起来两三千字，每条依据带一千多字符的原文片段。
   这里做三件事让它读得动：
   - 正文按小节切开，太长的小节先收起一半（「展开」看全部），段首的小标签加粗；
   - 依据按「论断」归并：同一句论断被几篇论文支持，只写一遍论断，下面列论文和原文；
   - 右侧（窄屏在上方）一份目录，点了就到，滚动时标出读到哪。
   ============================================================ */

/* ---------------- 可收起的长块 ---------------- */

/** 收起时的高度；内容高出这个值的一倍半才收（只多出几行的不值得多点一下）。 */
const COLLAPSED_PX = 232;

export function Collapsible({
  children,
  open,
  onToggle,
  moreLabel,
}: {
  children: ReactNode;
  open: boolean;
  onToggle: () => void;
  moreLabel?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [tall, setTall] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setTall(el.scrollHeight > COLLAPSED_PX * 1.5);
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const clamped = tall && !open;
  return (
    <div className="idea-fold" data-fold={tall ? (open ? 'open' : 'closed') : 'short'}>
      <div ref={ref} className={'idea-fold-body' + (clamped ? ' is-clamped' : '')} style={clamped ? { maxHeight: COLLAPSED_PX } : undefined}>
        {children}
      </div>
      {tall && (
        <button type="button" className="step-toggle idea-fold-toggle" aria-expanded={open} onClick={onToggle}>
          {open ? tr('收起', 'Show less') : (moreLabel ?? tr('展开全文', 'Show all'))}
        </button>
      )}
    </div>
  );
}

/* ---------------- 正文小节 ---------------- */

/** 渲染前的整理：行内编号列举拆成列表，段首小标签加粗。 */
export function tidyBody(md: string): string {
  return boldLeadLabels(splitInlineEnumerations(md));
}

export function useBodySections(text: string): BodySection[] {
  return useMemo(() => splitBodySections(text), [text]);
}

export function PlanSections({
  sections,
  renderPaperRef,
  anchorPrefix,
}: {
  sections: BodySection[];
  renderPaperRef?: PaperRefRenderer;
  anchorPrefix: string;
}) {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const allOpen = sections.length > 0 && sections.every((s) => open.has(s.id));
  // 从目录点过来的小节直接展开
  useEffect(() => {
    const onHash = (e: Event) => {
      const id = (e as CustomEvent<string>).detail;
      if (typeof id === 'string' && id.startsWith(anchorPrefix)) setOpen((prev) => new Set(prev).add(id.slice(anchorPrefix.length)));
    };
    window.addEventListener('idea-outline-jump', onHash);
    return () => window.removeEventListener('idea-outline-jump', onHash);
  }, [anchorPrefix]);

  if (sections.length === 0) return null;
  const single = sections.length === 1 && !sections[0]!.title;
  return (
    <div className="idea-body">
      {!single && sections.length > 1 && (
        <button
          type="button"
          className="step-toggle idea-body-all"
          data-testid="plan-expand-all"
          onClick={() => setOpen(allOpen ? new Set() : new Set(sections.map((s) => s.id)))}
        >
          {allOpen ? tr('全部收起', 'Collapse all') : tr('全部展开', 'Expand all')}
        </button>
      )}
      {sections.map((s) => (
        <div key={s.id} className="idea-sub" id={anchorPrefix + s.id} data-sub={s.title}>
          {s.title && <h3 className="idea-sub-title">{s.title}</h3>}
          <Collapsible
            open={open.has(s.id)}
            moreLabel={tr(`展开全文（约 ${s.chars} 字）`, 'Show all')}
            onToggle={() =>
              setOpen((prev) => {
                const next = new Set(prev);
                if (next.has(s.id)) next.delete(s.id);
                else next.add(s.id);
                return next;
              })
            }
          >
            <Markdown source={tidyBody(s.body)} renderPaperRef={renderPaperRef} />
          </Collapsible>
        </div>
      ))}
    </div>
  );
}

/* ---------------- 文献依据：按论断归并 ---------------- */

export interface ClaimGroup {
  claim: string;
  items: IdeaEvidence[];
}

/** 同一句论断的几条依据并成一组（保持第一次出现的顺序）。没写论断的各自成组。 */
export function groupByClaim(list: IdeaEvidence[]): ClaimGroup[] {
  const out: ClaimGroup[] = [];
  const index = new Map<string, ClaimGroup>();
  for (const ev of list) {
    const claim = (ev.why ?? '').trim();
    const hit = claim ? index.get(claim) : undefined;
    if (hit) hit.items.push(ev);
    else {
      const g = { claim, items: [ev] };
      out.push(g);
      if (claim) index.set(claim, g);
    }
  }
  return out;
}

function Excerpt({ raw, title }: { raw: string; title?: string }) {
  const [open, setOpen] = useState(false);
  const snip = useMemo(() => cleanSnippet(raw, title), [raw, title]);
  if (!snip.text) return null;
  const long = snip.text.length > 260;
  return (
    <blockquote className={'idea-ev-snippet' + (long && !open ? ' is-clamped' : '')} data-testid="evidence-excerpt">
      {snip.text}
      {long && (
        <button type="button" className="step-toggle idea-ev-more" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? tr('收起', 'Show less') : tr('展开原文', 'Show the full passage')}
        </button>
      )}
    </blockquote>
  );
}

function PaperSource({ ev }: { ev: IdeaEvidence }) {
  const snippet = typeof ev.snippet === 'string' ? ev.snippet.trim() : '';
  return (
    <li className="idea-ev-paperrow">
      <div className="idea-ev-src">
        {ev.paper_id ? (
          <Link to={readerHref(ev.paper_id, snippet)} className="idea-ev-paper" title={snippet ? tr('打开论文，跳到这段原文', 'Open the paper at this passage') : ev.title}>
            {ev.title}
          </Link>
        ) : ev.url ? (
          <a href={ev.url} target="_blank" rel="noreferrer noopener" className="idea-ev-paper" title={ev.title}>
            {ev.title}
          </a>
        ) : (
          <span className="idea-ev-paper" title={ev.title}>
            {ev.title}
          </span>
        )}
        {!ev.paper_id && <span className="idea-ev-tag">{ev.source === 'external' ? tr('不在文献库里', 'Not in your library') : tr('趋势信号', 'Trend signal')}</span>}
      </div>
      {snippet && <Excerpt raw={snippet} title={ev.title} />}
    </li>
  );
}

export function ClaimList({ list }: { list: IdeaEvidence[] }) {
  const groups = useMemo(() => groupByClaim(list), [list]);
  return (
    <ul className="idea-ev-list">
      {groups.map((g, i) => (
        <li key={i} className="idea-ev-item" data-papers={g.items.length}>
          {g.claim && <div className="idea-ev-why">{g.claim}</div>}
          <ul className="idea-ev-papers">
            {g.items.map((ev, j) => (
              <PaperSource key={`${ev.paper_id ?? ev.title}-${j}`} ev={ev} />
            ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}

/* ---------------- 页内目录 ---------------- */

export interface OutlineItem {
  id: string;
  label: string;
  /** 小节（缩进一级） */
  sub?: boolean;
  /** 右侧的小数字（几条依据…） */
  count?: number;
}

/** 目录：点了滚到那一节；滚动时标出当前读到的一节。 */
export function IdeaOutline({ items }: { items: OutlineItem[] }) {
  const [active, setActive] = useState<string | null>(items[0]?.id ?? null);
  const ids = items.map((i) => i.id).join('|');
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const els = ids
      .split('|')
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => !!el);
    if (els.length === 0) return;
    const visible = new Map<string, number>();
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) visible.set(e.target.id, e.boundingClientRect.top);
          else visible.delete(e.target.id);
        }
        // 当前节 = 视口上半部分里最靠上的那一节
        let best: string | null = null;
        let top = Infinity;
        for (const [id, y] of visible) {
          if (y < top) {
            top = y;
            best = id;
          }
        }
        if (best) setActive(best);
      },
      { rootMargin: '-72px 0px -55% 0px', threshold: 0 },
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, [ids]);

  if (items.length < 3) return null;
  const jump = (id: string) => {
    window.dispatchEvent(new CustomEvent('idea-outline-jump', { detail: id }));
    const el = document.getElementById(id);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setActive(id);
  };
  return (
    <nav className="idea-outline" aria-label={tr('目录', 'On this page')} data-testid="idea-outline">
      <div className="idea-outline-title">{tr('目录', 'On this page')}</div>
      <ul>
        {items.map((it) => (
          <li key={it.id} className={(it.sub ? 'is-sub ' : '') + (active === it.id ? 'is-on' : '')}>
            <button type="button" onClick={() => jump(it.id)} aria-current={active === it.id ? 'true' : undefined}>
              <span>{it.label}</span>
              {typeof it.count === 'number' && it.count > 0 && <span className="idea-outline-n">{it.count}</span>}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
