import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { CompileBadge } from '../../components/ui/CompileBadge';
import { FigureEmbed, usePaperFigures } from '../../components/ui/FigureGallery';
import { Markdown } from '../../lib/markdown';
import { api, type ShelfItemRead, type ShelfWikiSource } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { libraryPath, useLibraries } from '../libraries/hooks';
import { readerFrom } from '../reading/shared';

/* ============================================================
   相关研究 · 右栏详情（与「我的文献库」LibraryDetailPane 同一版式）：
   - 顶部：解读状态徽标 + venue，标题 + 作者，主操作行
     （打开阅读页 / arXiv / 生成 wiki / 刷新快照 / 移出）；
   - 课题备注「为什么相关」：多行编辑、停止输入后自动保存；
   - frontmatter 风格元数据卡（含加入时间与来源）；
   - TL;DR / 摘要（摘要取自论文详情接口）；
   - wiki 正文：列表接口已按 库版实时 > 个人版 > 快照 解析好，
     直接渲染 markdown（双链 → 来源方向库，嵌图取论文详情）。
   ============================================================ */

/* ---------------- wiki 来源徽标（四态统一） ---------------- */

// 模块级常量不调 tr()：保留 zh/en 字段，渲染处再 tr
const BADGE: Record<
  ShelfWikiSource,
  { zh: string; en: string; fg: string; bg: string; tipZh: string; tipEn: string }
> = {
  live: {
    zh: '库版解读',
    en: 'Library wiki',
    fg: 'var(--accent-text)',
    bg: 'var(--accent-soft)',
    tipZh: '显示方向文献库的实时解读，库里更新会自动跟着变。',
    tipEn: 'Live wiki from the shared direction library; follows library updates automatically.',
  },
  personal: {
    zh: '个人版解读',
    en: 'Personal wiki',
    fg: 'var(--violet-tx)',
    bg: 'var(--violet-bg)',
    tipZh: '库里没有这篇的解读，显示的是你自己生成的个人版。',
    tipEn: 'No library wiki for this paper; showing the personal version you generated.',
  },
  snapshot: {
    zh: '快照解读',
    en: 'Snapshot wiki',
    fg: 'var(--warn-tx)',
    bg: 'var(--warn-bg)',
    tipZh: '库版解读已不可用（被移除或重编译），显示的是添加时保存的快照，可手动刷新。',
    tipEn: 'The library wiki is gone (removed or recompiled); showing the snapshot saved when added. You can refresh it.',
  },
  none: {
    zh: '暂无解读',
    en: 'No wiki',
    fg: 'var(--text-3)',
    bg: 'var(--surface-3)',
    tipZh: '这篇论文还没有任何解读，可以用 AI 生成个人版。',
    tipEn: 'No wiki for this paper yet — you can generate a personal one with AI.',
  },
};

/** 快照日期 → 「7 月 22 日」 / "Jul 22"。 */
export function fmtSnapshotDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return tr(
    `${d.getMonth() + 1} 月 ${d.getDate()} 日`,
    d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
  );
}

/** 完整日期 → 「2026 年 7 月 22 日」 / "Jul 22, 2026"。 */
function fmtDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return tr(
    `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日`,
    d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }),
  );
}

/** wiki 来源徽标：compact 用于列表行（更小、快照不带日期）。 */
export function WikiBadge({
  source,
  snapshotAt,
  compact,
}: {
  source: ShelfWikiSource;
  snapshotAt?: string | null;
  compact?: boolean;
}) {
  const b = BADGE[source];
  const label =
    !compact && source === 'snapshot'
      ? tr(`${b.zh} · ${fmtSnapshotDate(snapshotAt ?? null)}`, `${b.en} · ${fmtSnapshotDate(snapshotAt ?? null)}`)
      : tr(b.zh, b.en);
  return (
    <span
      className="mono"
      title={tr(b.tipZh, b.tipEn)}
      style={{
        fontSize: compact ? 10 : 10.5,
        color: b.fg,
        background: b.bg,
        padding: compact ? '1px 7px' : '2px 9px',
        borderRadius: 999,
        flexShrink: 0,
        whiteSpace: 'nowrap',
      }}
    >
      {label}
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
          {tr('为什么相关', 'Why relevant')}
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
  generating,
  onGenerateWiki,
  refreshing,
  onRefreshSnapshot,
  onShelf = true,
  onAdd,
  addPending = false,
}: {
  item: ShelfItemRead;
  notePending: boolean;
  onSaveNote: (note: string | null) => void;
  removePending: boolean;
  onRemove: () => void;
  /** 本篇的个人版 wiki 正在生成 */
  generating: boolean;
  onGenerateWiki: () => void;
  /** 本篇的快照正在刷新 */
  refreshing: boolean;
  onRefreshSnapshot: () => void;
  /** 是否已在相关研究书架内。false（语义检索命中的语料论文尚未收藏）时隐藏
      备注 / 移出 / 生成解读等书架专属操作，改为「加入相关研究」。 */
  onShelf?: boolean;
  onAdd?: () => void;
  addPending?: boolean;
}) {
  const navigate = useNavigate();
  const location = useLocation();

  // 摘要 / TL;DR / 嵌图 / 编译信息来自论文详情（与阅读页、文献追踪同 queryKey 缓存互通）
  const paperQuery = useQuery({
    queryKey: ['paper', item.paper_id],
    queryFn: () => api.getPaper(item.paper_id),
    retry: false,
  });
  const paper = paperQuery.data;

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

  // [[概念]] 双链 → 来源方向库的概念页；个人补充（无来源库）退到库列表
  const onWikiLink = useCallback(
    (name: string) =>
      navigate(
        item.source_library_id
          ? libraryPath(item.source_library_id, `?concept=${encodeURIComponent(name)}`)
          : '/libraries',
      ),
    [navigate, item.source_library_id],
  );

  const authors = item.authors.map((a) => a.name).join(', ');
  const tldr = item.tldr ?? paper?.tldr ?? null;
  const abstract = paper?.abstract ?? null;
  const arxivUrl = item.arxiv_id ? `https://arxiv.org/abs/${item.arxiv_id}` : null;

  const wikiLabel =
    item.wiki_source === 'live'
      ? tr('AI 图文介绍 · 库版', 'AI intro · library')
      : item.wiki_source === 'personal'
        ? tr('AI 图文介绍 · 个人版', 'AI intro · personal')
        : tr(
            `AI 图文介绍 · ${fmtSnapshotDate(item.snapshot_at)}快照`,
            `AI intro · snapshot ${fmtSnapshotDate(item.snapshot_at)}`,
          );

  return (
    <div className="scroll fadeup" key={item.paper_id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      {/* —— pills 行：解读状态 + venue —— */}
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        <WikiBadge source={item.wiki_source} snapshotAt={item.snapshot_at} />
        {item.venue && (
          <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
            {item.venue}
          </span>
        )}
      </div>

      {/* —— 标题 + 作者 —— */}
      <h1 style={{ fontSize: 20, fontWeight: 680, lineHeight: 1.3, margin: '0 0 6px', letterSpacing: '-0.01em' }}>
        {item.title}
      </h1>
      {authors && <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.6 }}>{authors}</div>}

      {/* —— 操作行 —— */}
      <div className="row gap8 wrap" style={{ marginTop: 14 }}>
        <button
          className="btn btn-primary sm"
          onClick={() => navigate(`/papers/${item.paper_id}/read`, { state: readerFrom(location, 'research') })}
        >
          <Icon name="file" size={13} />
          {tr('打开阅读页', 'Open reader')}
        </button>
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
        {onShelf && item.wiki_source === 'none' && (
          <button
            className="btn btn-soft sm"
            title={tr('用 AI 生成这篇论文的个人版解读（使用你的模型额度）', 'Generate a personal wiki with AI (uses your model quota)')}
            disabled={generating}
            onClick={onGenerateWiki}
          >
            <Icon name="sparkle" size={13} />
            {generating ? tr('生成中…', 'Generating…') : tr('生成 wiki', 'Generate wiki')}
          </button>
        )}
        {onShelf && item.wiki_source === 'snapshot' && (
          <button
            className="btn btn-soft sm"
            title={tr(
              '重新拷一份当前可得的最新解读（库版优先，其次个人版）',
              'Re-copy the latest available wiki (library first, then personal)',
            )}
            disabled={refreshing}
            onClick={onRefreshSnapshot}
          >
            <Icon name="refresh" size={13} />
            {refreshing ? tr('刷新中…', 'Refreshing…') : tr('刷新快照', 'Refresh snapshot')}
          </button>
        )}
        {onShelf ? (
          <button
            className="btn btn-ghost sm"
            title={tr('移出相关研究（个人库收藏保留）', 'Remove from related work (kept in my library)')}
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

      {/* —— 课题备注：为什么相关（仅书架内论文） —— */}
      {onShelf && <NoteEditor key={item.paper_id} note={item.note} pending={notePending} onSave={onSaveNote} />}

      {/* —— frontmatter 风格元数据卡 —— */}
      <div className="card card-pad" style={{ margin: '18px 0 0', background: 'var(--surface-2)', padding: '14px 18px' }}>
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
      </div>

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

      {/* —— wiki 正文（库版实时 > 个人版 > 快照，接口已解析好） —— */}
      {item.wiki_content ? (
        <div style={{ marginTop: 22 }}>
          <div
            className="row gap8"
            style={{ paddingBottom: 10, marginBottom: 16, borderBottom: '0.5px solid var(--border)' }}
          >
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
              {wikiLabel}
            </span>
            {item.wiki_source === 'live' && <CompileBadge model={paper?.compiled_model} at={paper?.compiled_at} />}
          </div>
          <Markdown source={item.wiki_content} onWikiLink={onWikiLink} renderFigure={renderFigure} />
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
            {onShelf
              ? tr(
                  '这篇论文还没有解读。可以用 AI 生成一份个人版（使用你的模型额度）。',
                  'No wiki for this paper yet. Generate a personal one with AI (uses your model quota).',
                )
              : tr(
                  '这篇论文还没有解读。加入相关研究后可以用 AI 生成个人版。',
                  'No wiki for this paper yet. Add it to related work to generate a personal one with AI.',
                )}
          </div>
          {onShelf && (
            <button
              className="btn btn-soft sm"
              style={{ marginTop: 10 }}
              disabled={generating}
              onClick={onGenerateWiki}
            >
              <Icon name="sparkle" size={13} />
              {generating ? tr('生成中…', 'Generating…') : tr('生成 wiki', 'Generate wiki')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
