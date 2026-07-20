import { createElement, useMemo, type ReactNode } from 'react';
import katex from 'katex';
import 'katex/dist/katex.min.css';

/* ============================================================
   轻量安全 markdown 渲染器（数学公式外零依赖）。
   - 输出 React 元素而非 innerHTML：原文中的 HTML 一律按纯文本
     渲染，天然免疫 XSS，无需 sanitizer（KaTeX 输出为库生成的
     受控 HTML，是唯一的 innerHTML 注入点）。
   - 支持：# 标题、无序/有序列表、**粗体**、*斜体*、`行内代码`、
     ``` 代码块、表格、> 引用、--- 分隔线、[文字](http链接)。
   - 数学公式（KaTeX）：行内 `$...$` / `\(...\)`，块级 `$$...$$`
     （可多行，独立成段）；解析失败按原文显示，不抛错。
   - 扩展：`[[概念名]]` / `[[概念名|别名]]` 双链 → 可点击 chip，
     通过 onWikiLink(概念名) 回调（Research Wiki 用）。
   - 扩展：独立一行的 `![[fig:N]]` 图片标记（docs/api-lit.md §6.6）
     → 调用 renderFigure(N) 渲染嵌入图；未提供 renderFigure 或
     返回 null 时该行静默跳过。行内出现的同标记也剥除不显示；
     代码块 / 行内代码内不解析。
   样式见 global.css 的 .md / .wikilink。
   ============================================================ */

/** KaTeX 渲染（错误容忍：非法 TeX 原样显示为红色文本而不抛错）。 */
function MathSpan({ tex, display }: { tex: string; display: boolean }) {
  const html = useMemo(
    () =>
      katex.renderToString(tex, {
        displayMode: display,
        throwOnError: false,
        strict: false,
        output: 'html',
      }),
    [tex, display],
  );
  if (display) {
    return (
      <div
        className="md-math-block"
        style={{ margin: '10px 0', overflowX: 'auto', textAlign: 'center' }}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    );
  }
  return <span dangerouslySetInnerHTML={{ __html: html }} />;
}

export type WikiLinkHandler = (name: string) => void;

/** `![[fig:N]]` 图片标记渲染回调；返回 null 表示不渲染（该行跳过）。 */
export type FigureRenderer = (index: number) => ReactNode;

/**
 * `[[paper:uuid]]` 库内论文引用渲染回调（docs/api-idea2.md §6）；
 * 返回 null 时回退为普通 [[双链]] chip 行为。
 */
export type PaperRefRenderer = (paperId: string) => ReactNode;

/**
 * 行内 `[[fig:论文uuid:图号]]` 跨论文配图渲染回调（文献库对话用）；
 * 返回 null 时回退为普通 [[双链]] chip 行为。
 */
export type LibraryFigureRenderer = (paperId: string, index: number) => ReactNode;

/**
 * `[n]`（1-2 位数字）引用角标渲染回调（文献库对话用，编号对应来源清单）；
 * 返回 null 时按普通文本渲染。未提供回调时 `[n]` 不做任何解析。
 */
export type CitationRenderer = (index: number) => ReactNode;

export interface MarkdownProps {
  source: string;
  onWikiLink?: WikiLinkHandler;
  /** 渲染独立一行的 `![[fig:N]]` 嵌入图 */
  renderFigure?: FigureRenderer;
  /** 渲染行内 `[[paper:uuid]]` 库内论文引用 */
  renderPaperRef?: PaperRefRenderer;
  /** 渲染行内 `[[fig:论文uuid:图号]]` 跨论文配图（文献库对话） */
  renderLibraryFigure?: LibraryFigureRenderer;
  /** 渲染行内 `[n]` 引用角标（文献库对话） */
  renderCitation?: CitationRenderer;
  className?: string;
  style?: React.CSSProperties;
}

/* ---------------- inline ---------------- */

const INLINE_RE =
  /(`[^`\n]+`)|(!\[\[fig:\d+\]\])|(\[\[([^\]|\n]+)(?:\|([^\]\n]+))?\]\])|(\[([^\]\n]*)\]\((https?:\/\/[^\s)]+)\))|(\*\*([^*\n]+)\*\*)|(\*([^*\n]+)\*)|(~~([^~\n]+)~~)|(\[(\d{1,2})\])|(\$([^\s$](?:[^$\n]*?[^\s$])?)\$)|(\\\((.+?)\\\))/g;

function renderInline(
  text: string,
  onWikiLink?: WikiLinkHandler,
  renderPaperRef?: PaperRefRenderer,
  renderCitation?: CitationRenderer,
  renderLibraryFigure?: LibraryFigureRenderer,
): ReactNode[] {
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
    } else if (m[15] !== undefined) {
      // [n] 引用角标：交给 renderCitation；无回调 / 返回 null 时按原文输出
      const node = renderCitation?.(Number(m[16])) ?? null;
      if (node !== null && node !== undefined && node !== false) {
        out.push(<span key={k++}>{node}</span>);
      } else {
        out.push(m[15]);
      }
    } else if (m[3] !== undefined) {
      const target = (m[4] ?? '').trim();
      const label = (m[5] ?? target).trim();
      // [[fig:论文uuid:图号]] 跨论文配图：交给 renderLibraryFigure；返回 null 时回退普通双链
      if (renderLibraryFigure && target.toLowerCase().startsWith('fig:')) {
        const rest = target.slice('fig:'.length);
        const ci = rest.lastIndexOf(':');
        const idx = ci > 0 ? Number(rest.slice(ci + 1)) : NaN;
        if (ci > 0 && Number.isInteger(idx)) {
          const node = renderLibraryFigure(rest.slice(0, ci).trim(), idx);
          if (node !== null && node !== undefined && node !== false) {
            out.push(<span key={k++}>{node}</span>);
            last = re.lastIndex;
            continue;
          }
        }
      }
      // [[paper:uuid]] 库内论文引用：交给 renderPaperRef；无回调 / 返回 null 时回退普通双链
      if (renderPaperRef && target.toLowerCase().startsWith('paper:')) {
        const node = renderPaperRef(target.slice('paper:'.length).trim());
        if (node !== null && node !== undefined && node !== false) {
          out.push(<span key={k++}>{node}</span>);
          last = re.lastIndex;
          continue;
        }
      }
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
      out.push(<strong key={k++}>{renderInline(m[10] ?? '', onWikiLink, renderPaperRef, renderCitation, renderLibraryFigure)}</strong>);
    } else if (m[11] !== undefined) {
      out.push(<em key={k++}>{renderInline(m[12] ?? '', onWikiLink, renderPaperRef, renderCitation, renderLibraryFigure)}</em>);
    } else if (m[13] !== undefined) {
      out.push(<del key={k++}>{m[14] ?? ''}</del>);
    } else if (m[17] !== undefined) {
      out.push(<MathSpan key={k++} tex={m[18] ?? ''} display={false} />);
    } else if (m[19] !== undefined) {
      out.push(<MathSpan key={k++} tex={m[20] ?? ''} display={false} />);
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
/** 块级公式起始：$$… 或 \[… */
const RE_MATH_OPEN = /^\s*(\$\$|\\\[)/;

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

function parseBlocks(
  src: string,
  onWikiLink?: WikiLinkHandler,
  renderFigure?: FigureRenderer,
  renderPaperRef?: PaperRefRenderer,
  renderCitation?: CitationRenderer,
  renderLibraryFigure?: LibraryFigureRenderer,
): ReactNode[] {
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

    // 块级公式：$$…$$ / \[…\]（可多行）
    const mathOpen = RE_MATH_OPEN.exec(line);
    if (mathOpen) {
      const delim = mathOpen[1] === '$$' ? '$$' : '\\]';
      let first = line.trim().slice(2);
      const texLines: string[] = [];
      if (first.endsWith(delim) && first.length > delim.length) {
        texLines.push(first.slice(0, -delim.length));
        i++;
      } else {
        if (first.endsWith(delim)) first = first.slice(0, -delim.length);
        texLines.push(first);
        i++;
        while (i < lines.length) {
          const l = (lines[i] ?? '').trim();
          i++;
          if (l.endsWith(delim)) {
            texLines.push(l.slice(0, -delim.length));
            break;
          }
          texLines.push(l);
        }
      }
      const tex = texLines.join('\n').trim();
      if (tex) out.push(<MathSpan key={key++} tex={tex} display />);
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
      out.push(createElement(`h${lvl}`, { key: key++ }, renderInline(h[2] ?? '', onWikiLink, renderPaperRef, renderCitation, renderLibraryFigure)));
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
                  <th key={ci}>{renderInline(c, onWikiLink, renderPaperRef, renderCitation, renderLibraryFigure)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {header.map((_, ci) => (
                    <td key={ci}>{renderInline(r[ci] ?? '', onWikiLink, renderPaperRef, renderCitation, renderLibraryFigure)}</td>
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
      out.push(<blockquote key={key++}>{parseBlocks(buf.join('\n'), onWikiLink, renderFigure, renderPaperRef, renderCitation, renderLibraryFigure)}</blockquote>);
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
      const children = items.map((it, ii) => <li key={ii}>{renderInline(it, onWikiLink, renderPaperRef, renderCitation, renderLibraryFigure)}</li>);
      out.push(ordered ? <ol key={key++}>{children}</ol> : <ul key={key++}>{children}</ul>);
      continue;
    }

    // 段落：连续非空、非块起始行；行间软换行渲染为 <br/>
    const buf: string[] = [line];
    i++;
    while (i < lines.length) {
      const l = lines[i] ?? '';
      if (!l.trim() || isBlockStart(l) || RE_HR.test(l) || RE_FIG_LINE.test(l) || RE_MATH_OPEN.test(l)) break;
      if (l.includes('|') && RE_TABLE_SEP.test(lines[i + 1] ?? '')) break;
      buf.push(l);
      i++;
    }
    out.push(
      <p key={key++}>
        {buf.map((l, li) => (
          <span key={li}>
            {li > 0 && <br />}
            {renderInline(l, onWikiLink, renderPaperRef, renderCitation, renderLibraryFigure)}
          </span>
        ))}
      </p>,
    );
  }
  return out;
}

/** 安全 markdown 渲染（含 [[概念]] 双链与 ![[fig:N]] 嵌入图）。 */
export function Markdown({
  source,
  onWikiLink,
  renderFigure,
  renderPaperRef,
  renderLibraryFigure,
  renderCitation,
  className,
  style,
}: MarkdownProps) {
  const nodes = useMemo(
    () => parseBlocks(source, onWikiLink, renderFigure, renderPaperRef, renderCitation, renderLibraryFigure),
    [source, onWikiLink, renderFigure, renderPaperRef, renderCitation, renderLibraryFigure],
  );
  return (
    <div className={`md${className ? ` ${className}` : ''}`} style={style}>
      {nodes}
    </div>
  );
}
