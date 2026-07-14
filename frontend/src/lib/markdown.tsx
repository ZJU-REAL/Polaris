import { createElement, useMemo, type ReactNode } from 'react';

/* ============================================================
   轻量安全 markdown 渲染器（零依赖）。
   - 输出 React 元素而非 innerHTML：原文中的 HTML 一律按纯文本
     渲染，天然免疫 XSS，无需 sanitizer。
   - 支持：# 标题、无序/有序列表、**粗体**、*斜体*、`行内代码`、
     ``` 代码块、表格、> 引用、--- 分隔线、[文字](http链接)。
   - 扩展：`[[概念名]]` / `[[概念名|别名]]` 双链 → 可点击 chip，
     通过 onWikiLink(概念名) 回调（Research Wiki 用）。
   - 扩展：独立一行的 `![[fig:N]]` 图片标记（docs/api-lit.md §6.6）
     → 调用 renderFigure(N) 渲染嵌入图；未提供 renderFigure 或
     返回 null 时该行静默跳过。行内出现的同标记也剥除不显示；
     代码块 / 行内代码内不解析。
   样式见 global.css 的 .md / .wikilink。
   ============================================================ */

export type WikiLinkHandler = (name: string) => void;

/** `![[fig:N]]` 图片标记渲染回调；返回 null 表示不渲染（该行跳过）。 */
export type FigureRenderer = (index: number) => ReactNode;

export interface MarkdownProps {
  source: string;
  onWikiLink?: WikiLinkHandler;
  /** 渲染独立一行的 `![[fig:N]]` 嵌入图 */
  renderFigure?: FigureRenderer;
  className?: string;
  style?: React.CSSProperties;
}

/* ---------------- inline ---------------- */

const INLINE_RE =
  /(`[^`\n]+`)|(!\[\[fig:\d+\]\])|(\[\[([^\]|\n]+)(?:\|([^\]\n]+))?\]\])|(\[([^\]\n]*)\]\((https?:\/\/[^\s)]+)\))|(\*\*([^*\n]+)\*\*)|(\*([^*\n]+)\*)|(~~([^~\n]+)~~)/g;

function renderInline(text: string, onWikiLink?: WikiLinkHandler): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let k = 0;
  const re = new RegExp(INLINE_RE.source, 'g');
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] !== undefined) {
      out.push(<code key={k++}>{m[1].slice(1, -1)}</code>);
    } else if (m[2] !== undefined) {
      // 行内出现的 ![[fig:N]] 图片标记：剥除不显示（容错，只有独立成行才渲染图）
    } else if (m[3] !== undefined) {
      const target = (m[4] ?? '').trim();
      const label = (m[5] ?? target).trim();
      out.push(
        <span
          key={k++}
          className="wikilink"
          role="link"
          tabIndex={0}
          title={`[[${target}]]`}
          onClick={() => onWikiLink?.(target)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onWikiLink?.(target);
          }}
        >
          {label}
        </span>,
      );
    } else if (m[6] !== undefined) {
      out.push(
        <a key={k++} href={m[8]} target="_blank" rel="noreferrer noopener">
          {m[7] || m[8]}
        </a>,
      );
    } else if (m[9] !== undefined) {
      out.push(<strong key={k++}>{renderInline(m[10] ?? '', onWikiLink)}</strong>);
    } else if (m[11] !== undefined) {
      out.push(<em key={k++}>{renderInline(m[12] ?? '', onWikiLink)}</em>);
    } else if (m[13] !== undefined) {
      out.push(<del key={k++}>{m[14] ?? ''}</del>);
    }
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/* ---------------- blocks ---------------- */

const RE_HEADING = /^(#{1,6})\s+(.*)$/;
const RE_HR = /^\s*([-*_])\s*(?:\1\s*){2,}$/;
const RE_UL = /^\s*[-*+]\s+(.*)$/;
const RE_OL = /^\s*\d+[.)]\s+(.*)$/;
const RE_QUOTE = /^\s*>\s?(.*)$/;
const RE_TABLE_SEP = /^\s*\|?\s*:?-{2,}[-\s:|]*$/;
/** 独立一行的 ![[fig:N]] 图片标记（docs/api-lit.md §6.6） */
const RE_FIG_LINE = /^\s*!\[\[fig:(\d+)\]\]\s*$/;

function isBlockStart(line: string): boolean {
  return (
    RE_HEADING.test(line) ||
    RE_UL.test(line) ||
    RE_OL.test(line) ||
    RE_QUOTE.test(line) ||
    line.trim().startsWith('```')
  );
}

function splitTableRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

function parseBlocks(src: string, onWikiLink?: WikiLinkHandler, renderFigure?: FigureRenderer): ReactNode[] {
  const lines = src.replace(/\r\n/g, '\n').split('\n');
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i] ?? '';
    if (!line.trim()) {
      i++;
      continue;
    }

    // ``` 代码块
    if (line.trim().startsWith('```')) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !(lines[i] ?? '').trim().startsWith('```')) {
        buf.push(lines[i] ?? '');
        i++;
      }
      i++; // 跳过闭合 ```
      out.push(
        <pre key={key++} className="codeblock">
          {buf.join('\n')}
        </pre>,
      );
      continue;
    }

    // 独立一行的 ![[fig:N]] 嵌入图：交给 renderFigure；无回调或返回 null 则该行静默跳过
    const figMatch = RE_FIG_LINE.exec(line);
    if (figMatch) {
      const node = renderFigure?.(Number(figMatch[1])) ?? null;
      if (node !== null && node !== undefined && node !== false) {
        out.push(
          <div key={key++} className="md-figure" style={{ margin: '14px 0' }}>
            {node}
          </div>,
        );
      }
      i++;
      continue;
    }

    // 标题
    const h = RE_HEADING.exec(line);
    if (h) {
      const lvl = (h[1] ?? '#').length;
      out.push(createElement(`h${lvl}`, { key: key++ }, renderInline(h[2] ?? '', onWikiLink)));
      i++;
      continue;
    }

    // 分隔线（在表格判定之后无冲突：--- 单独成行且上一行非表头）
    if (RE_HR.test(line)) {
      out.push(<hr key={key++} />);
      i++;
      continue;
    }

    // 表格：当前行含 |，下一行是分隔行
    if (line.includes('|') && RE_TABLE_SEP.test(lines[i + 1] ?? '') && (lines[i + 1] ?? '').includes('|')) {
      const header = splitTableRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && (lines[i] ?? '').includes('|') && (lines[i] ?? '').trim()) {
        rows.push(splitTableRow(lines[i] ?? ''));
        i++;
      }
      out.push(
        <div key={key++} className="md-table-wrap">
          <table>
            <thead>
              <tr>
                {header.map((c, ci) => (
                  <th key={ci}>{renderInline(c, onWikiLink)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {header.map((_, ci) => (
                    <td key={ci}>{renderInline(r[ci] ?? '', onWikiLink)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    // 引用块
    if (RE_QUOTE.test(line)) {
      const buf: string[] = [];
      while (i < lines.length) {
        const q = RE_QUOTE.exec(lines[i] ?? '');
        if (!q) break;
        buf.push(q[1] ?? '');
        i++;
      }
      out.push(<blockquote key={key++}>{parseBlocks(buf.join('\n'), onWikiLink, renderFigure)}</blockquote>);
      continue;
    }

    // 无序 / 有序列表（扁平渲染）
    if (RE_UL.test(line) || RE_OL.test(line)) {
      const ordered = RE_OL.test(line);
      const re = ordered ? RE_OL : RE_UL;
      const items: string[] = [];
      while (i < lines.length) {
        const it = re.exec(lines[i] ?? '');
        if (!it) break;
        items.push(it[1] ?? '');
        i++;
      }
      const children = items.map((it, ii) => <li key={ii}>{renderInline(it, onWikiLink)}</li>);
      out.push(ordered ? <ol key={key++}>{children}</ol> : <ul key={key++}>{children}</ul>);
      continue;
    }

    // 段落：连续非空、非块起始行；行间软换行渲染为 <br/>
    const buf: string[] = [line];
    i++;
    while (i < lines.length) {
      const l = lines[i] ?? '';
      if (!l.trim() || isBlockStart(l) || RE_HR.test(l) || RE_FIG_LINE.test(l)) break;
      if (l.includes('|') && RE_TABLE_SEP.test(lines[i + 1] ?? '')) break;
      buf.push(l);
      i++;
    }
    out.push(
      <p key={key++}>
        {buf.map((l, li) => (
          <span key={li}>
            {li > 0 && <br />}
            {renderInline(l, onWikiLink)}
          </span>
        ))}
      </p>,
    );
  }
  return out;
}

/** 安全 markdown 渲染（含 [[概念]] 双链与 ![[fig:N]] 嵌入图）。 */
export function Markdown({ source, onWikiLink, renderFigure, className, style }: MarkdownProps) {
  const nodes = useMemo(
    () => parseBlocks(source, onWikiLink, renderFigure),
    [source, onWikiLink, renderFigure],
  );
  return (
    <div className={`md${className ? ` ${className}` : ''}`} style={style}>
      {nodes}
    </div>
  );
}
