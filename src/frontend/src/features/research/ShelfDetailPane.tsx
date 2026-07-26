import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { CompileBadge } from '../../components/ui/CompileBadge';
import {
  FigureEmbed,
  FiguresSection,
  hasEmbeddedFigures,
  usePaperFigures,
} from '../../components/ui/FigureGallery';
import { ScoreRing } from '../../components/ui/ScoreRing';
import { Markdown } from '../../lib/markdown';
import { api, type ReadingStatus, type ShelfItemRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { libraryPath, useLibraries } from '../libraries/hooks';
import { readerFrom } from '../reading/shared';
import { PaperReader } from '../wiki/PaperReader';
import { AffiliationChips, AuthorLinks, MetaFold, usePoolConceptNav } from '../wiki/shared';
import {
  ConceptChips,
  PaperMyMetaRow,
  PaperMyTagsRow,
  PaperNotesSection,
  WikiHeaderActions,
} from '../shared/PaperDetailBlocks';

/* ============================================================
   相关研究 · 右栏详情（与我的文献库LibraryDetailPane 同一版式）：
   - 顶部：解读状态徽标 + venue，标题 + 作者，主操作行
     （打开阅读页 / arXiv / 移出）；
   - 课题备注为什么相关：多行编辑、停止输入后自动保存；
   - frontmatter 风格元数据卡（含加入时间与来源）；
   - TL;DR / 摘要（摘要取自论文详情接口）；
   - wiki 正文：解读每篇一份，接口直接给，
     直接渲染 markdown（双链 → 不限库的概念页，嵌图取论文详情）。
   ============================================================ */

/** 完整日期 → 「2026 年 7 月 22 日」 / "Jul 22, 2026"。 */
function fmtDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return tr(
    `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日`,
    d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }),
  );
}

/** 这条书架行有没有解读：后端给了 has_wiki 就信它（语义命中的行只有这个信号，
    正文要进详情才拉）；旧后端没这个字段时退回看正文。 */
export function shelfHasWiki(item: ShelfItemRead): boolean {
  return item.has_wiki ?? !!item.wiki_content;
}

/** 解读状态徽标：已有解读 / 暂无解读；compact 用于列表行。 */
export function WikiBadge({ hasWiki, compact }: { hasWiki: boolean; compact?: boolean }) {
  return (
    <span
      className="mono"
      style={{
        fontSize: compact ? 10 : 10.5,
        color: hasWiki ? 'var(--accent-text)' : 'var(--text-3)',
        background: hasWiki ? 'var(--accent-soft)' : 'var(--surface-3)',
        padding: compact ? '1px 7px' : '2px 9px',
        borderRadius: 999,
        flexShrink: 0,
        whiteSpace: 'nowrap',
      }}
    >
      {hasWiki ? tr('已有解读', 'Has wiki') : tr('暂无解读', 'No wiki')}
    </span>
  );
}

/* ---------------- 课题备注：多行 + 自动保存 ---------------- */

const NOTE_SAVE_DELAY = 1000;

function NoteEditor({
  note,
  pending,
  onSave,
}: {
  note: string | null;
  pending: boolean;
  onSave: (note: string | null) => void;
}) {
  const [draft, setDraft] = useState(note ?? '');
  const timerRef = useRef<number | null>(null);

  // 最新 draft / note / onSave 存 ref：定时器与卸载兜底里用，避免闭包过期
  const stateRef = useRef({ draft, note, onSave });
  stateRef.current = { draft, note, onSave };

  const commit = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const s = stateRef.current;
    const next = s.draft.trim() || null;
    if (next !== (s.note ?? null)) s.onSave(next);
  }, []);

  // 卸载（切换选中论文）时把没落盘的改动补交
  useEffect(() => commit, [commit]);

  const onChange = (v: string) => {
    setDraft(v);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(commit, NOTE_SAVE_DELAY);
  };

  const dirty = (draft.trim() || null) !== (note ?? null);
  const status = pending
    ? tr('保存中…', 'Saving…')
    : dirty
      ? tr('停下来会自动保存', 'Auto-saves when you pause')
      : note
        ? tr('已保存', 'Saved')
        : '';

  return (
    <div
      style={{
        marginTop: 18,
        padding: '12px 16px 10px',
        borderRadius: 10,
        background: 'var(--surface-2)',
      }}
    >
      <div className="row gap8">
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent-text)', letterSpacing: '0.04em' }}>
          {tr('课题备注 · 为什么相关', 'Topic note · why relevant')}
        </span>
        <span className="mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-4)', flexShrink: 0 }}>
          {status}
        </span>
      </div>
      <textarea
        value={draft}
        onChange={(e) => onChange(e.target.value)}
        onBlur={commit}
        placeholder={tr('写一句为什么相关…', 'Write a line on why this matters…')}
        style={{
          width: '100%',
          minHeight: 56,
          marginTop: 6,
          padding: 0,
          border: 'none',
          outline: 'none',
          background: 'transparent',
          resize: 'vertical',
          fontFamily: 'var(--sans)',
          fontSize: 12.5,
          lineHeight: 1.65,
          color: 'var(--text-2)',
        }}
      />
    </div>
  );
}

/* ---------------- 详情面板 ---------------- */

/** frontmatter 风格元数据行（同「我的文献库」详情面板版式）。 */
function MetaItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="row" style={{ gap: 12, padding: '4px 0', alignItems: 'flex-start' }}>
      <span className="mono" style={{ fontSize: 11, color: 'var(--accent-text)', width: 88, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 12.5, color: 'var(--text-2)', flex: 1, minWidth: 0, overflowWrap: 'break-word' }}>
        {children}
      </span>
    </div>
  );
}

export function ShelfDetailPane({
  item,
  notePending,
  onSaveNote,
  removePending,
  onRemove,
  onShelf = true,
  onAdd,
  addPending = false,
  onFilterAuthor,
  onFilterAffiliation,
}: {
  item: ShelfItemRead;
  notePending: boolean;
  onSaveNote: (note: string | null) => void;
  removePending: boolean;
  onRemove: () => void;
  /** 是否已在相关研究书架内。false（语义检索命中的语料论文尚未收藏）时隐藏
      备注 / 移出 等书架专属操作，改为加入相关研究。 */
  onShelf?: boolean;
  onAdd?: () => void;
  addPending?: boolean;
  /** 点作者 → 按该作者过滤列表；不传则作者名不可点 */
  onFilterAuthor?: (name: string) => void;
  /** 点机构 chip → 按该机构过滤列表；不传则 chips 不可点 */
  onFilterAffiliation?: (name: string) => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [readerOpen, setReaderOpen] = useState(false);
  // 阅览模式打开后是否直接唤起打印（「导出 PDF」一步直达）
  const [readerPrint, setReaderPrint] = useState(false);
  const openReader = useCallback(() => {
    setReaderPrint(false);
    setReaderOpen(true);
  }, []);
  const openReaderForPrint = useCallback(() => {
    setReaderPrint(true);
    setReaderOpen(true);
  }, []);

  // 摘要 / TL;DR / 嵌图 / 编译信息 / 星标 / 标签来自论文详情（与阅读页、文献追踪同 queryKey 缓存互通）
  const detailKey = useMemo(() => ['paper', item.paper_id], [item.paper_id]);
  const paperQuery = useQuery({
    queryKey: detailKey,
    queryFn: () => api.getPaper(item.paper_id),
    retry: false,
  });
  const paper = paperQuery.data;

  // 星标 / 阅读状态改完刷新书架列表与检索结果；笔记只影响详情里的计数
  const listKeys = useMemo(() => [['shelf'], ['shelf-search']], []);
  const noteKeys = useMemo(() => [detailKey], [detailKey]);

  // 来源方向库名（列表小且 5 分钟缓存，直接查全量列表）
  const libsQuery = useLibraries({}, item.source_library_id !== null);
  const sourceLib = item.source_library_id
    ? (libsQuery.data?.find((l) => l.id === item.source_library_id) ?? null)
    : null;

  // 正文 ![[fig:N]] 嵌入图（同文献追踪）
  const figures = usePaperFigures(paper);
  const renderFigure = useCallback(
    (n: number) => {
      const fig = figures.find((f) => f.index === n);
      return fig && paper ? <FigureEmbed paperId={paper.id} fig={fig} /> : null;
    },
    [figures, paper],
  );

  // 相关研究里的都是池级论文：概念一律进不限库的概念页
  const { openConcept, openConceptByName } = usePoolConceptNav();

  // 机构：书架条目自带（后端从内容池论文取）；旧后端没这个字段时退回论文详情
  const affiliations = item.affiliations ?? paper?.affiliations;
  const tldr = item.tldr ?? paper?.tldr ?? null;
  const abstract = paper?.abstract ?? null;
  const arxivUrl = item.arxiv_id ? `https://arxiv.org/abs/${item.arxiv_id}` : null;
  const relevance = paper?.relevance_score ?? null;
  const readingStatus: ReadingStatus = paper?.reading_status ?? 'unread';

  const wikiLabel = tr('AI 图文介绍', 'AI intro');

  return (
    <div className="scroll fadeup" key={item.paper_id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      {/* —— pills 行：解读状态 + venue —— */}
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        <WikiBadge hasWiki={shelfHasWiki(item)} />
        {item.venue && (
          <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
            {item.venue}
          </span>
        )}
      </div>

      {/* —— 标题 + 作者 + 机构（都可点：点了按它过滤书架）+ 相关度 —— */}
      <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 style={{ fontSize: 20, fontWeight: 680, lineHeight: 1.3, margin: '0 0 6px', letterSpacing: '-0.01em' }}>
            {item.title}
          </h1>
          <AuthorLinks authors={item.authors} onFilter={onFilterAuthor} />
          <AffiliationChips affiliations={affiliations} onFilter={onFilterAffiliation} />
        </div>
        {relevance !== null && <ScoreRing value={relevance} max={1} size={56} label={tr('相关度', 'Relevance')} />}
      </div>

      {/* —— 操作行 —— */}
      <div className="row gap8 wrap" style={{ marginTop: 14 }}>
        <button
          className="btn btn-primary sm"
          onClick={() => navigate(`/papers/${item.paper_id}/read`, { state: readerFrom(location, 'research') })}
        >
          <Icon name="file" size={13} />
          {tr('打开阅读页', 'Open reader')}
        </button>
        {paper && item.wiki_content && (
          <button
            className="btn btn-soft sm"
            title={tr('全屏阅览图文介绍，可导出 PDF', 'Full-screen reading view, exportable to PDF')}
            onClick={openReader}
          >
            <Icon name="book" size={13} />
            {tr('阅览模式', 'Reading mode')}
          </button>
        )}
        {item.source_library_id ? (
          <button
            className="btn btn-ghost sm"
            title={tr('打开这篇所在的方向文献库', 'Open the direction library this paper lives in')}
            onClick={() =>
              navigate(libraryPath(item.source_library_id ?? '', `?paper=${item.paper_id}`))
            }
          >
            <Icon name="book" size={13} />
            {tr('去文献库', 'Open library')}
          </button>
        ) : (
          // 手动添加、未纳入任何方向文献库：置灰不可点，hover 说明原因
          <span title={tr('这篇是手动添加的，未纳入公共文献库', 'Manually added — not in any shared library')}>
            <button className="btn btn-ghost sm" disabled style={{ opacity: 0.45, cursor: 'not-allowed' }}>
              <Icon name="book" size={13} />
              {tr('去文献库', 'Open library')}
            </button>
          </span>
        )}
        {arxivUrl && (
          <a
            className="btn btn-ghost sm"
            href={arxivUrl}
            target="_blank"
            rel="noreferrer noopener"
            style={{ textDecoration: 'none' }}
          >
            <Icon name="link" size={13} />
            arXiv
          </a>
        )}
        {item.url && !arxivUrl && (
          <a
            className="btn btn-ghost sm"
            href={item.url}
            target="_blank"
            rel="noreferrer noopener"
            style={{ textDecoration: 'none' }}
          >
            <Icon name="link" size={13} />
            {tr('原文链接', 'Source link')}
          </a>
        )}
        {onShelf ? (
          <button
            className="btn btn-ghost sm"
            title={tr(
              '移出相关研究，放进回收站，之后可以召回（个人库收藏保留）',
              'Remove from related work — goes to the trash and can be restored (kept in my library)',
            )}
            disabled={removePending}
            onClick={onRemove}
            style={{ marginLeft: 'auto', color: 'var(--danger-tx)' }}
          >
            <Icon name="trash" size={13} />
            {tr('移出', 'Remove')}
          </button>
        ) : onAdd ? (
          <button
            className="btn btn-primary sm"
            title={tr('把这篇加入相关研究（同时收藏进个人库）', 'Add to related work (also saved to my library)')}
            disabled={addPending}
            onClick={onAdd}
            style={{ marginLeft: 'auto' }}
          >
            <Icon name="plus" size={13} />
            {addPending ? tr('加入中…', 'Adding…') : tr('加入相关研究', 'Add to related work')}
          </button>
        ) : null}
      </div>

      {/* —— 个人状态：星标 + 阅读状态（跟文献库里是同一条记录） —— */}
      {paper && (
        <PaperMyMetaRow
          paperId={item.paper_id}
          starred={paper.starred ?? false}
          readingStatus={readingStatus}
          detailKey={detailKey}
          invalidateKeys={listKeys}
        />
      )}

      {/* —— 我的标签：就地改，只有自己看得到 —— */}
      {/* 库标签的界面入口已移除，个人标签取代了它；后端端点与数据保留。 */}
      {paper && (
        <PaperMyTagsRow
          paperId={item.paper_id}
          myTags={paper.my_tags}
          detailKey={detailKey}
          invalidateKeys={listKeys}
        />
      )}

      {/* —— 课题备注：为什么相关（仅书架内论文；这条是课题里公开的说明） —— */}
      {onShelf && <NoteEditor key={item.paper_id} note={item.note} pending={notePending} onSave={onSaveNote} />}

      {/* —— 我的笔记（只有自己看得到，和上面的课题备注是两回事） —— */}
      {paper && (
        <PaperNotesSection
          paperId={item.paper_id}
          noteCount={paper.note_count ?? 0}
          invalidateKeys={noteKeys}
        />
      )}

      {/* —— frontmatter 风格元信息（默认折叠） —— */}
      <MetaFold>
        <MetaItem label="arxiv_id">
          {item.arxiv_id ? <span className="mono">{item.arxiv_id}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="doi">
          {item.doi ? <span className="mono">{item.doi}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label={tr('年份', 'year')}>
          {item.year !== null ? <span className="mono">{item.year}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label={tr('发表于', 'venue')}>{item.venue ?? <span className="muted">—</span>}</MetaItem>
        <MetaItem label={tr('加入时间', 'added')}>
          <span className="mono">{fmtDay(item.added_at)}</span>
        </MetaItem>
        <MetaItem label={tr('来源', 'source')}>
          {item.source_library_id ? (
            <button
              onClick={() => navigate(libraryPath(item.source_library_id ?? ''))}
              style={{
                border: 'none',
                background: 'transparent',
                padding: 0,
                cursor: 'pointer',
                fontSize: 12.5,
                fontFamily: 'var(--sans)',
                color: 'var(--accent-text)',
              }}
            >
              {sourceLib ? sourceLib.name : tr('方向文献库', 'Direction library')}
            </button>
          ) : (
            tr('手动添加', 'Added manually')
          )}
        </MetaItem>
      </MetaFold>

      {/* —— 概念 chips：点了进不限库的概念页 —— */}
      <ConceptChips concepts={paper?.concepts} onOpen={openConcept} />

      {/* —— TL;DR —— */}
      {tldr && (
        <div
          style={{
            marginTop: 18,
            padding: '12px 16px',
            borderRadius: 10,
            background: 'var(--accent-soft)',
            fontSize: 13,
            lineHeight: 1.65,
            color: 'var(--text)',
          }}
        >
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent-text)', display: 'block', marginBottom: 4 }}>
            TL;DR
          </span>
          {tldr}
        </div>
      )}

      {/* —— 摘要 —— */}
      {abstract && (
        <div style={{ marginTop: 18 }}>
          <div className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em', marginBottom: 6 }}>
            {tr('摘要', 'Abstract')}
          </div>
          <div style={{ fontSize: 12.5, lineHeight: 1.7, color: 'var(--text-2)' }}>{abstract}</div>
        </div>
      )}

      {/* —— 重要图片画廊（只读：提取/重新提取是库维护动作） —— */}
      {paper && (
        <FiguresSection
          paper={paper}
          readOnly
          defaultCollapsed={hasEmbeddedFigures(item.wiki_content, figures)}
        />
      )}

      {/* —— wiki 正文（解读每篇一份，接口直接给） —— */}
      {item.wiki_content ? (
        <div style={{ marginTop: 22 }}>
          <div
            className="row gap8"
            style={{ paddingBottom: 10, marginBottom: 16, borderBottom: '0.5px solid var(--border)' }}
          >
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
              {wikiLabel}
            </span>
            <CompileBadge model={paper?.compiled_model} at={paper?.compiled_at} />
            {paper && (
              <WikiHeaderActions
                onRead={openReader}
                onExport={openReaderForPrint}
                style={{ marginLeft: 'auto' }}
              />
            )}
          </div>
          <Markdown source={item.wiki_content} onWikiLink={openConceptByName} renderFigure={renderFigure} />
        </div>
      ) : (
        <div
          style={{
            marginTop: 22,
            padding: '18px 20px',
            borderRadius: 10,
            border: '1px dashed var(--border-2)',
            textAlign: 'center',
          }}
        >
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.6 }}>
            {tr('这篇还没有 AI 解读。', 'No AI wiki for this paper yet.')}
          </div>
        </div>
      )}

      {readerOpen && paper && (
        <PaperReader
          paper={paper}
          /* 正文来自书架条目（与论文详情同一份解读），显式传入省一次等待 */
          wikiContent={item.wiki_content}
          renderFigure={renderFigure}
          onWikiLink={openConceptByName}
          autoPrint={readerPrint}
          onClose={() => setReaderOpen(false)}
        />
      )}
    </div>
  );
}
