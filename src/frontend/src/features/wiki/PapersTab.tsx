import { memo, useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { EmptyState, LoadingState } from '../../components/ui/EmptyState';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { DotsMenu, type DotsMenuItem } from '../../components/ui/DotsMenu';
import { FigureEmbed, FiguresSection, hasEmbeddedFigures, usePaperFigures } from '../../components/ui/FigureGallery';
import { CompileBadge } from '../../components/ui/CompileBadge';
import { citationExportItems, ExportDropdown } from '../../components/ui/ExportDropdown';
import { PaperReader } from './PaperReader';
import { readerFrom, READING_STATUS, readingStatusMeta } from '../reading/shared';
import { evidenceCitationRenderer, evidenceRefs } from '../reading/evidenceArtifact';
import { toast } from '../../components/ui/Toast';
import { canSendToExtension, sendLibraryPapersToExtension } from '../../lib/extension-download-batches';
import { extensionDeliveryAvailable } from '../../lib/polaris-extension';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { fmtTime, parseApiDate } from '../../lib/format';
import { errorText } from '../../lib/errors';
import { useIsCompact } from '../../lib/useBreakpoint';
import {
  api,
  ApiError,
  type CitationFormat,
  type MyMeta,
  type PaperDetail,
  type PaperRead,
  type PaperSort,
  type PaperStatusFilter,
  type ReadingStatus,
  type SearchMode,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { usePendingByPaper } from '../../lib/pending';
import { AuthorLinks, categoryMeta, MetaItem, saveBlob, SearchInput, useDebounced } from './shared';
import { PaperCitationsSection, PaperExtractionsSection, PaperMyTagChips, PaperMyTagsRow, PaperNotesSection } from '../shared/PaperDetailBlocks';
import { PaperRow, relevanceLabel, relevanceWords, shortAuthors } from '../shared/PaperRow';
import { PaperDetailTabs, PaperFact, PaperFacts, PaperKeyPoints, PaperKicker, splitSummary } from '../shared/PaperDetail';
import { PaperFulltextFact } from '../shared/PaperFulltext';
import { TrashModal, type TrashItemView } from '../shared/TrashModal';
import { AddToButton } from '../library/AddToPopover';
import { paperDragProps } from '../assistant/paperDrag';
import { ComparisonModal } from './ComparisonModal';

/* ============================================================
   文献库的「论文」页签：左边安静的列表，右边一篇论文的详情。

   列表：搜索 + 一行文字筛选（全部 / 有解读 / 已星标 / 新加入）+ 排序；
         更多条件收在筛选按钮里。
   详情：出处 · 标题 · 作者 → 一个主动作（阅读原文）→ 事实行（为什么在这里 /
         PDF 与全文 / 阅读状态 / 我的标签）→ 要点（只出现一次）→ 解读 / 摘要 / 笔记 / 更多。
   ============================================================ */

const PAGE_SIZE = 20;

/** 列表视图：全部 = 已收进来的论文；不相关的进回收站，不显示不计数。 */
type ViewFilter = 'all' | 'compiled' | 'starred' | 'today';

// 模块级常量存 zh/en 两份文案，渲染处再 tr（import 时求值不会随语言切换更新）
const VIEW_FILTERS: { v: ViewFilter; zh: string; en: string; hintZh: string; hintEn: string }[] = [
  { v: 'all', zh: '全部', en: 'All', hintZh: '这个文献库的全部论文', hintEn: 'Every paper in this library' },
  { v: 'compiled', zh: '有解读', en: 'Summarized', hintZh: '已生成解读的论文', hintEn: 'Papers with a summary' },
  { v: 'starred', zh: '已星标', en: 'Starred', hintZh: '加了星标的论文', hintEn: 'Papers you starred' },
  { v: 'today', zh: '新加入', en: 'New', hintZh: '上次更新加入的论文', hintEn: 'Papers added by the last update' },
];

/** 视图 → 列表查询参数（不相关 / 回收站里的论文一律不出现）。 */
function viewQuery(view: ViewFilter): {
  status: PaperStatusFilter;
  starred?: boolean;
  created_from?: string;
  daily_only?: boolean;
  last_sync_only?: boolean;
} {
  if (view === 'compiled') return { status: 'compiled_any' };
  if (view === 'starred') return { status: 'library', starred: true };
  // 新加入：上次更新带进来的那批，不按「今天」卡（上次更新在昨天就永远是 0 篇）
  if (view === 'today') return { status: 'library', last_sync_only: true };
  return { status: 'library' };
}

/** 深链 ?author= / ?affiliation= 带进来的筛选条件；seq 递增触发重新应用 */
export interface AdvSearchSeed {
  author?: string;
  affiliation?: string;
  seq: number;
}

export interface PapersTabProps {
  pid?: string;
  libraryId?: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onOpenConcept: (id: string) => void;
  /** [[概念名]] 点击 → 按名称跳概念 */
  onWikiLink: WikiLinkHandler;
  /** 深链带入的作者/机构筛选（阅读页跳回文献库用） */
  advSeed?: AdvSearchSeed | null;
  /** 空库时「找论文」去哪 */
  onFindPapers?: () => void;
}

/* ---------------- 导出（放在文献库页的「更多」菜单里） ---------------- */

export function useLibraryExport(libraryId: string): { items: DotsMenuItem[]; busy: boolean } {
  const obsidianMutation = useMutation({
    mutationFn: () => api.downloadLibraryObsidianExport(libraryId),
    onSuccess: (blob) => {
      saveBlob(blob, 'polaris-library.zip');
      toast(tr('已导出 Obsidian 笔记库', 'Obsidian vault exported'), 'ok');
    },
    onError: (e) => toast(`${tr('无法导出：', 'Couldn’t export: ')}${errorText(e)}`, 'error'),
  });
  const citationsMutation = useMutation({
    mutationFn: (format: CitationFormat) => api.downloadLibraryCitations(libraryId, { format }),
    onSuccess: (blob, format) => {
      saveBlob(blob, format === 'bibtex' ? 'polaris-references.bib' : 'polaris-references.json');
      toast(format === 'bibtex' ? tr('已导出 BibTeX', 'BibTeX exported') : tr('已导出 CSL-JSON', 'CSL-JSON exported'), 'ok');
    },
    onError: (e) => toast(`${tr('无法导出：', 'Couldn’t export: ')}${errorText(e)}`, 'error'),
  });
  return {
    busy: obsidianMutation.isPending || citationsMutation.isPending,
    items: [
      { key: 'bibtex', icon: 'download', label: tr('导出全部引用（BibTeX）', 'Export citations (BibTeX)'), onSelect: () => citationsMutation.mutate('bibtex') },
      { key: 'csl', icon: 'download', label: tr('导出全部引用（CSL-JSON）', 'Export citations (CSL-JSON)'), onSelect: () => citationsMutation.mutate('csl-json') },
      { key: 'obsidian', icon: 'download', label: tr('导出为 Obsidian 笔记库', 'Export as an Obsidian vault'), onSelect: () => obsidianMutation.mutate() },
    ],
  };
}

/* ---------------- 回收站 ---------------- */

/** 回收站原因标签：打分淘汰 = 不相关；否则视为手动删除（老数据缺字段时按分数推断）。 */
function trashReasonOf(p: PaperRead): 'irrelevant' | 'manual' {
  if (p.trash_reason === 'manual' || p.trash_reason === 'irrelevant') return p.trash_reason;
  return p.relevance_score !== null ? 'irrelevant' : 'manual';
}

/** 论文库回收站：弹窗外壳复用共享 TrashModal，端点与行映射留在这里。 */
export function PapersTrashModal({ pid, libraryId, open, onClose }: { pid: string; libraryId?: string; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const scopeId = libraryId ?? pid;

  const trashQuery = useQuery({
    queryKey: ['papers-trash', scopeId],
    queryFn: () =>
      libraryId
        ? api.listLibraryPapersFull(libraryId, { status: 'excluded', size: 100, sort: '-published_at' })
        : api.listPapers(pid, { status: 'excluded', size: 100, sort: '-published_at' }),
    enabled: open,
    retry: false,
  });
  const papers = useMemo(() => trashQuery.data?.items ?? [], [trashQuery.data]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['papers-trash', scopeId] });
    void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
    void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
    void queryClient.invalidateQueries({ queryKey: ['project-graph', scopeId] });
  };

  const restoreMutation = useMutation({
    // 作用域召回：锁定当前库那份成员行，避免跨库误召回（见彻底删除同理）
    mutationFn: (id: string) =>
      libraryId ? api.restoreLibraryPaper(libraryId, id) : api.restoreProjectPaper(pid, id),
    onSuccess: (p) => {
      toast(`${tr('已恢复：', 'Restored: ')}${p.title.slice(0, 30)}`, 'ok');
      invalidate();
    },
    onError: (e) =>
      toast(`${tr('无法恢复：', 'Couldn’t restore: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const purgeMutation = useMutation({
    // 作用域彻底删除：只删当前库那份成员行；无库作用域会命中错误的库、删不掉本库这份
    mutationFn: (id: string) =>
      libraryId ? api.deleteLibraryPaper(libraryId, id) : api.deleteProjectPaper(pid, id),
    onSuccess: () => {
      toast(tr('已彻底删除', 'Permanently deleted'), 'ok');
      invalidate();
    },
    onError: (e) =>
      toast(`${tr('无法删除：', 'Couldn’t delete: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const emptyMutation = useMutation({
    mutationFn: () => (libraryId ? api.emptyLibraryTrash(libraryId) : api.emptyTrash(pid)),
    onSuccess: (res) => {
      toast(tr(`已清空回收站，共 ${res.deleted} 篇`, `Trash emptied: ${res.deleted} papers`), 'ok');
      invalidate();
    },
    onError: (e) =>
      toast(`${tr('无法清空：', 'Couldn’t empty the trash: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // 论文行 → 共享回收站行的展示模型（相关度条 + 删除原因标签 + tldr）
  const items = useMemo<TrashItemView[]>(
    () =>
      papers.map((p) => ({
        id: p.id,
        code: p.arxiv_id ?? p.venue ?? '—',
        year: p.year,
        title: p.title,
        leading: p.has_wiki ? <Icon name="sparkle" size={11} style={{ color: 'var(--accent)' }} /> : undefined,
        aside: <RelevanceBar value={p.relevance_score} />,
        tags:
          trashReasonOf(p) === 'irrelevant' ? (
            <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
              {tr('不相关', 'Not relevant')}
            </span>
          ) : (
            <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
              {tr('手动删除', 'Deleted manually')}
            </span>
          ),
        desc: p.tldr,
        searchText: [p.title, ...p.authors.map((a) => a.name)].join('\n'),
      })),
    [papers],
  );

  return (
    <TrashModal
      open={open}
      onClose={onClose}
      sub={tr(
        '不相关或手动删除的论文',
        'Papers marked not relevant or deleted by you',
      )}
      items={items}
      total={trashQuery.data?.total}
      loading={trashQuery.isLoading}
      busy={restoreMutation.isPending || purgeMutation.isPending || emptyMutation.isPending}
      emptying={emptyMutation.isPending}
      onRestore={(id) => restoreMutation.mutate(id)}
      onPurge={(id) => purgeMutation.mutate(id)}
      onEmpty={() => emptyMutation.mutate()}
      emptyWarning={(n) =>
        tr(
          `${n} 篇论文及其文件将被永久删除。`,
          `${n} papers and their files will be permanently deleted.`,
        )
      }
    />
  );
}

/* ---------------- 列表行 ---------------- */

/* memo：父组件（大量筛选/选中 state）任一变更都会触发全列表重渲染。
   忽略函数 props 的比较是安全的：两个 handler 只捕获稳定引用与 p.id。 */
const LibraryPaperRow = memo(function LibraryPaperRow({
  p,
  active,
  checked,
  selectMode,
  onClick,
  onToggleCheck,
}: {
  p: PaperRead;
  active: boolean;
  checked: boolean;
  selectMode: boolean;
  onClick: () => void;
  onToggleCheck: () => void;
}) {
  const reading = p.reading_status && p.reading_status !== 'unread' ? readingStatusMeta(p.reading_status) : null;
  return (
    <PaperRow
      title={p.title}
      meta={[shortAuthors(p.authors), p.year, relevanceLabel(p.relevance_score), reading ? tr(reading.label, reading.en) : null, shortVenue(p.venue)]}
      summary={p.tldr}
      summaryFallback={p.has_wiki ? undefined : tr('还没有解读', 'No summary yet')}
      starred={p.starred}
      noteCount={p.note_count}
      marks={<PaperMyTagChips myTags={p.my_tags} />}
      active={active}
      onClick={onClick}
      // 可以直接拖给助手解读
      rowProps={paperDragProps(p.id, p.title)}
      leading={
        selectMode ? (
          <input
            type="checkbox"
            checked={checked}
            aria-label={tr('选择这篇论文', 'Select this paper')}
            onClick={(e) => e.stopPropagation()}
            onChange={onToggleCheck}
          />
        ) : undefined
      }
    />
  );
}, (prev, next) =>
  prev.p === next.p && prev.active === next.active && prev.checked === next.checked && prev.selectMode === next.selectMode,
);

/** arXiv 分类号（cs.CL）不是期刊，行里不写；真正的期刊/会议名太长时截一下。 */
export function shortVenue(venue: string | null | undefined): string | null {
  if (!venue) return null;
  if (/^[a-z-]+(\.[A-Za-z-]{2,})?$/.test(venue)) return null;
  return venue.length > 48 ? `${venue.slice(0, 46)}…` : venue;
}

/* ---------------- 详情面板 ---------------- */

/** 概念 chips 默认最多展示数，超出折叠 */
const CONCEPT_CHIP_LIMIT = 12;

type DetailSection = 'summary' | 'abstract' | 'notes' | 'more';

function PaperDetailPane({
  paperId,
  pid,
  libraryId,
  onOpenConcept,
  onWikiLink,
  onFilterAuthor,
  onFilterAffiliation,
  onDeleted,
  onBack,
  compiling,
  onRecompile,
  sendingToExtension,
  onSendToExtension,
}: {
  paperId: string;
  pid: string;
  libraryId?: string;
  onOpenConcept: (id: string) => void;
  onWikiLink: WikiLinkHandler;
  onFilterAuthor: (name: string) => void;
  onFilterAffiliation: (name: string) => void;
  onDeleted: () => void;
  /** 窄屏：回到列表 */
  onBack: () => void;
  /** 这篇正在生成解读（状态存在列表页，切走再切回来照样是「生成中」） */
  compiling: boolean;
  onRecompile: (paperId: string) => void;
  sendingToExtension: boolean;
  onSendToExtension: (paper: PaperDetail) => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const scopeId = libraryId ?? pid;
  const [section, setSection] = useState<DetailSection | null>(null);
  const [conceptsOpen, setConceptsOpen] = useState(false);
  const [readerOpen, setReaderOpen] = useState(false);
  const [readerPrint, setReaderPrint] = useState(false);
  // 已有解读时重新生成要先确认：解读每篇只有一份，重新生成即覆盖
  const [recompileConfirm, setRecompileConfirm] = useState(false);

  // 作用域读：锁定当前库那份成员行，避免同一论文属多个库时读到跨库归并的错行
  const { data: paper, isLoading, isError } = useQuery({
    queryKey: ['paper', scopeId, paperId],
    queryFn: () =>
      libraryId
        ? api.getLibraryPaper(libraryId, paperId)
        : pid
          ? api.getProjectPaper(pid, paperId)
          : api.getPaper(paperId),
    retry: false,
  });

  const deleteMutation = useMutation({
    mutationFn: () =>
      libraryId ? api.batchDeleteLibraryPapers(libraryId, [paperId]) : api.batchDeletePapers(scopeId, [paperId]),
    onSuccess: () => {
      toast(tr('已移入回收站', 'Moved to trash'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['paper', scopeId, paperId] });
      void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['papers-trash', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['project-graph', scopeId] });
      onDeleted();
    },
    onError: (e) => toast(`${tr('无法删除：', 'Couldn’t delete: ')}${errorText(e)}`, 'error'),
  });

  const metaMutation = useMutation({
    mutationFn: (input: Partial<MyMeta>) => api.putMyMeta(paperId, input),
    onSuccess: (meta) => {
      queryClient.setQueryData<PaperDetail>(['paper', scopeId, paperId], (old) =>
        old ? { ...old, starred: meta.starred, reading_status: meta.reading_status } : old,
      );
      void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
    },
    onError: (e) => toast(`${tr('无法更新：', 'Couldn’t update: ')}${errorText(e)}`, 'error'),
  });

  // 换论文时收起本地开合状态（面板不随选中项重挂载，得自己收）
  useEffect(() => {
    setSection(null);
    setConceptsOpen(false);
    setReaderOpen(false);
    setRecompileConfirm(false);
  }, [paperId]);

  const figures = usePaperFigures(paper);
  const renderFigure = useCallback(
    (n: number) => {
      const fig = figures.find((f) => f.index === n);
      return fig ? <FigureEmbed paperId={paperId} fig={fig} /> : null;
    },
    [figures, paperId],
  );
  const refs = useMemo(() => evidenceRefs(paper?.wiki_evidence), [paper?.wiki_evidence]);
  const renderEvidenceCitation = useMemo(
    () => evidenceCitationRenderer({ libraryId, fallbackPaperId: paperId, title: paper?.title ?? '', refs }),
    [refs, libraryId, paper?.title, paperId],
  );
  // 解读开头的「TL;DR」就是上面的要点：正文里拿掉，只出现一次
  const split = useMemo(() => splitSummary(paper?.wiki_content), [paper?.wiki_content]);

  if (isLoading) return <LoadingState rows={6} />;
  if (isError || !paper) {
    return (
      <EmptyState
        compact
        icon="x"
        title={tr('无法加载这篇论文', 'Couldn’t load this paper')}
        desc={tr('它可能已被删除，或本机引擎未运行。', 'It may have been deleted, or the local engine isn’t running.')}
      />
    );
  }

  const arxivUrl = paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : null;
  const relevance = paper.relevance_score;
  const starred = paper.starred ?? false;
  const readingStatus: ReadingStatus = paper.reading_status ?? 'unread';
  const hasSummary = !!(paper.has_wiki && paper.wiki_content);
  const current: DetailSection = section ?? (hasSummary ? 'summary' : 'abstract');
  const openReader = () => navigate(`/papers/${paper.id}/read`, { state: readerFrom(location, 'wiki') });
  const added = paper.created_at ? fmtDay(paper.created_at) : null;

  const menu: DotsMenuItem[] = [
    {
      key: 'regen',
      icon: 'sparkle',
      label: hasSummary ? tr('重新生成解读', 'Regenerate summary') : tr('生成解读', 'Generate summary'),
      disabled: compiling,
      onSelect: () => (hasSummary ? setRecompileConfirm(true) : onRecompile(paperId)),
    },
    ...(hasSummary
      ? [
          {
            key: 'print',
            icon: 'download' as const,
            label: tr('把解读导出为 PDF', 'Export the summary as PDF'),
            onSelect: () => {
              setReaderPrint(true);
              setReaderOpen(true);
            },
          },
        ]
      : []),
    ...(arxivUrl || paper.url
      ? [
          {
            key: 'src',
            icon: 'link' as const,
            label: arxivUrl ? tr('在 arXiv 打开', 'Open on arXiv') : tr('打开原文页面', 'Open the source page'),
            separated: true,
            onSelect: () => window.open(arxivUrl ?? paper.url ?? '', '_blank', 'noopener'),
          },
        ]
      : []),
    ...(libraryId && extensionDeliveryAvailable()
      ? [
          {
            key: 'ext',
            icon: 'share' as const,
            label: tr('用浏览器扩展下载 PDF', 'Get the PDF via the browser extension'),
            disabled: sendingToExtension || !canSendToExtension(paper.status),
            title: canSendToExtension(paper.status) ? undefined : tr('加入本库后才能发送', 'Add it to the library first'),
            onSelect: () => onSendToExtension(paper),
          },
        ]
      : []),
    {
      key: 'del',
      icon: 'trash',
      label: tr('移入回收站', 'Move to trash'),
      danger: true,
      separated: true,
      disabled: deleteMutation.isPending,
      onSelect: () => deleteMutation.mutate(),
    },
  ];

  const sections: { key: DetailSection; label: string; count?: number }[] = [
    { key: 'summary', label: tr('解读', 'Summary') },
    { key: 'abstract', label: tr('摘要', 'Abstract') },
    { key: 'notes', label: tr('笔记', 'Notes'), count: paper.note_count ?? 0 },
    { key: 'more', label: tr('更多', 'More') },
  ];

  return (
    <div className="scroll fadeup pd-pane" key={paper.id} data-testid="paper-detail">
      <button type="button" className="btn btn-plain sm lib-back" onClick={onBack}>
        <Icon name="chevron" size={12} style={{ transform: 'rotate(180deg)' }} />
        {tr('返回列表', 'Back to the list')}
      </button>
      <PaperKicker year={paper.year} venue={shortVenue(paper.venue)} arxivId={paper.arxiv_id} doi={paper.doi} url={paper.url} />
      <h1 className="pd-title">{paper.title}</h1>
      <AuthorLinks authors={paper.authors} onFilter={onFilterAuthor} />
      {paper.affiliations && paper.affiliations.length > 0 && (
        <div className="pd-affil">
          {paper.affiliations.slice(0, 4).map((a, i) => (
            <span key={a}>
              {i > 0 && ' · '}
              <span
                className="author-link"
                role="button"
                tabIndex={0}
                title={tr(`只看 ${a} 的论文`, `Show only papers from ${a}`)}
                onClick={() => onFilterAffiliation(a)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') onFilterAffiliation(a);
                }}
              >
                {a}
              </span>
            </span>
          ))}
          {paper.affiliations.length > 4 &&
            tr(` 等 ${paper.affiliations.length} 家机构`, ` and ${paper.affiliations.length - 4} more`)}
        </div>
      )}

      <div className="pd-actions">
        <button className="btn btn-primary sm" onClick={openReader} data-testid="paper-read">
          <Icon name="file" size={13} />
          {tr('阅读原文', 'Read paper')}
        </button>
        <button
          className="btn btn-ghost sm"
          disabled={metaMutation.isPending}
          aria-pressed={starred}
          onClick={() => metaMutation.mutate({ starred: !starred })}
          style={starred ? { color: 'var(--warn-tx)' } : undefined}
        >
          <Icon name={starred ? 'starFill' : 'star'} size={13} />
          {starred ? tr('已加星标', 'Starred') : tr('加星标', 'Star')}
        </button>
        <AddToButton paperId={paper.id} text={tr('保存到…', 'Save to…')} />
        <DotsMenu items={menu} label={tr('这篇论文的更多操作', 'More for this paper')} />
      </div>

      <PaperFacts>
        <PaperFact label={tr('为什么在这里', 'Why it’s here')} testId="paper-why">
          <span>
            {relevance !== null && relevance !== undefined
              ? tr(`${relevanceWords(relevance)}（${relevance.toFixed(2)}）`, `${relevanceWords(relevance)} (${relevance.toFixed(2)})`)
              : tr('你自己加入的', 'You added it')}
          </span>
          {added && <span className="pd-fact-note">{tr(`${added}加入`, `added ${added}`)}</span>}
        </PaperFact>
        <PaperFact label={tr('PDF 与全文', 'PDF & full text')}>
          <PaperFulltextFact
            libraryId={libraryId}
            paperId={paper.id}
            pdfAvailable={!!paper.pdf_available}
            doi={paper.doi}
          />
        </PaperFact>
        <PaperFact label={tr('阅读状态', 'Reading')}>
          <div className="pd-seg" role="radiogroup" aria-label={tr('阅读状态', 'Reading status')}>
            {READING_STATUS.map((m) => (
              <button
                key={m.v}
                type="button"
                role="radio"
                aria-checked={readingStatus === m.v}
                className={'pl-qtab' + (readingStatus === m.v ? ' is-on' : '')}
                disabled={metaMutation.isPending}
                onClick={() => metaMutation.mutate({ reading_status: m.v })}
              >
                {tr(m.label, m.en)}
              </button>
            ))}
          </div>
        </PaperFact>
        <PaperFact label={tr('我的标签', 'My tags')}>
          <PaperMyTagsRow
            bare
            paperId={paper.id}
            myTags={paper.my_tags}
            detailKey={['paper', scopeId, paper.id]}
            invalidateKeys={[['papers', scopeId]]}
          />
        </PaperFact>
      </PaperFacts>

      {split.lead ? (
        <PaperKeyPoints>
          <Markdown source={split.lead} onWikiLink={onWikiLink} renderCitation={renderEvidenceCitation} />
        </PaperKeyPoints>
      ) : (
        <PaperKeyPoints tldr={paper.tldr} />
      )}

      <PaperDetailTabs tabs={sections} value={current} onChange={setSection} />

      {current === 'summary' &&
        (hasSummary ? (
          <div className="pd-prose">
            <div className="pd-byline">
              <span className="row gap8" style={{ minWidth: 0 }}>
                {compiling ? (
                  <span className="row gap6" style={{ color: 'var(--accent-text)' }}>
                    <Icon name="refresh" size={12} style={{ animation: 'spin 1s linear infinite' }} />
                    {tr('正在重新生成，约 1 分钟…', 'Regenerating, about a minute…')}
                  </span>
                ) : (
                  <>
                    <span>{tr('AI 生成的解读', 'AI-generated summary')}</span>
                    <CompileBadge model={paper.compiled_model} at={paper.compiled_at} />
                  </>
                )}
              </span>
              <button
                type="button"
                className="btn btn-plain xs"
                onClick={() => {
                  setReaderPrint(false);
                  setReaderOpen(true);
                }}
              >
                <Icon name="book" size={12} />
                {tr('全屏阅读', 'Full screen')}
              </button>
            </div>
            <Markdown
              source={split.body || paper.wiki_content || ''}
              onWikiLink={onWikiLink}
              renderFigure={renderFigure}
              renderCitation={renderEvidenceCitation}
            />
          </div>
        ) : (
          <div className="pd-empty" data-testid="paper-no-summary">
            <p>
              {compiling
                ? tr('正在生成解读，约 1 分钟…', 'Generating the summary, about a minute…')
                : tr('还没有解读。解读会把这篇论文的问题、方法和结果写成几段话。', 'No summary yet. A summary explains the problem, method and results in a few paragraphs.')}
            </p>
            {!compiling && (
              <button type="button" className="btn btn-soft sm" onClick={() => onRecompile(paperId)}>
                <Icon name="sparkle" size={13} />
                {tr('生成解读', 'Generate summary')}
              </button>
            )}
          </div>
        ))}

      {current === 'abstract' && (
        <div className="pd-abstract" data-testid="paper-abstract">
          {paper.abstract ? paper.abstract : <span className="muted">{tr('没有摘要', 'No abstract')}</span>}
        </div>
      )}

      {current === 'notes' && (
        <PaperNotesSection
          flat
          paperId={paper.id}
          noteCount={paper.note_count ?? 0}
          invalidateKeys={[['papers'], ['paper', paper.id], ['paper', scopeId, paper.id]]}
        />
      )}

      {current === 'more' && (
        <div className="pd-more">
          {paper.concepts.length > 0 && (
            <section>
              <div className="pd-more-label">{tr('涉及的概念', 'Concepts')}</div>
              <div className="row gap6 wrap">
                {(conceptsOpen ? paper.concepts : paper.concepts.slice(0, CONCEPT_CHIP_LIMIT)).map((c) => {
                  const meta = categoryMeta(c.category);
                  return (
                    <span
                      key={c.id}
                      className="wikilink"
                      style={{ background: meta.bg, color: meta.c, height: 24 }}
                      onClick={() => onOpenConcept(c.id)}
                    >
                      {c.name}
                      <span style={{ opacity: 0.6, marginLeft: 5, fontSize: '0.85em' }}>{tr(meta.zh, meta.en)}</span>
                    </span>
                  );
                })}
                {paper.concepts.length > CONCEPT_CHIP_LIMIT && (
                  <button type="button" className="btn btn-plain xs" onClick={() => setConceptsOpen((o) => !o)}>
                    {conceptsOpen
                      ? tr('收起', 'Show fewer')
                      : tr(`还有 ${paper.concepts.length - CONCEPT_CHIP_LIMIT} 个`, `${paper.concepts.length - CONCEPT_CHIP_LIMIT} more`)}
                  </button>
                )}
              </div>
            </section>
          )}
          <section>
            <div className="pd-more-label">{tr('编号与日期', 'Identifiers and dates')}</div>
            <MetaItem label={tr('arXiv 编号', 'arXiv ID')}>
              {paper.arxiv_id ? <span className="mono">{paper.arxiv_id}</span> : <span className="muted">—</span>}
            </MetaItem>
            <MetaItem label="DOI">{paper.doi ? <span className="mono">{paper.doi}</span> : <span className="muted">—</span>}</MetaItem>
            <MetaItem label={tr('发表日期', 'Published')}>
              {paper.published_at ? <span className="mono">{paper.published_at.slice(0, 10)}</span> : <span className="muted">—</span>}
            </MetaItem>
            <MetaItem label={tr('加入时间', 'Added')}>
              <span className="mono">{fmtTime(paper.created_at)}</span>
            </MetaItem>
          </section>
          <PaperExtractionsSection paperId={paper.id} />
          <PaperCitationsSection paperId={paper.id} />
          <FiguresSection paper={paper} defaultCollapsed={hasEmbeddedFigures(paper.wiki_content, figures)} />
        </div>
      )}

      {readerOpen && (
        <PaperReader
          paper={paper}
          libraryId={libraryId}
          renderFigure={renderFigure}
          onWikiLink={onWikiLink}
          onFilterAuthor={(name) => {
            setReaderOpen(false);
            onFilterAuthor(name);
          }}
          autoPrint={readerPrint}
          onClose={() => setReaderOpen(false)}
        />
      )}

      <ConfirmModal
        open={recompileConfirm}
        onClose={() => setRecompileConfirm(false)}
        title={tr('重新生成解读？', 'Regenerate the summary?')}
        message={tr('现有解读会被替换，无法恢复。', 'The current summary will be replaced and can’t be recovered.')}
        confirmText={tr('重新生成', 'Regenerate')}
        danger
        onConfirm={() => {
          setRecompileConfirm(false);
          onRecompile(paperId);
        }}
      />
    </div>
  );
}

function fmtDay(iso: string): string {
  const d = parseApiDate(iso);
  return tr(`${d.getMonth() + 1} 月 ${d.getDate()} 日`, d.toLocaleDateString('en', { month: 'short', day: 'numeric' }));
}

/* ---------------- Tab 主体 ---------------- */

const SORTS: { v: PaperSort; zh: string; en: string }[] = [
  { v: 'relevance', zh: '按相关度', en: 'By relevance' },
  { v: '-published_at', zh: '按发表时间', en: 'By publication date' },
  // 按加入时间：引擎的服务层支持 -created_at，但文献库列表接口的参数校验只放行前两种（会 422），先不提供
];

export function PapersTab({ pid, libraryId, selectedId, onSelect, onOpenConcept, onWikiLink, advSeed, onFindPapers }: PapersTabProps) {
  const scopeId = libraryId ?? pid ?? '';
  const narrow = useIsCompact();
  const [view, setView] = useState<ViewFilter>('all');
  const [sort, setSort] = useState<PaperSort>('relevance');
  const [mode, setMode] = useState<SearchMode>('keyword');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());

  const [myTagFilter, setMyTagFilter] = useState('');
  const [readingFilter, setReadingFilter] = useState<'' | ReadingStatus>('');
  // 更多筛选（搜索方式 / 作者 / 机构 / 发表时间 / 加入时间 / 标签 / 阅读状态）
  const [advOpen, setAdvOpen] = useState(false);
  const [advAuthor, setAdvAuthor] = useState('');
  const [advAffiliation, setAdvAffiliation] = useState('');
  const [advPubFrom, setAdvPubFrom] = useState('');
  const [advPubTo, setAdvPubTo] = useState('');
  const [advCreatedFrom, setAdvCreatedFrom] = useState('');
  const [advCreatedTo, setAdvCreatedTo] = useState('');
  const author = useDebounced(advAuthor.trim());
  const affiliation = useDebounced(advAffiliation.trim());
  const advActive = !!(
    author ||
    affiliation ||
    advPubFrom ||
    advPubTo ||
    advCreatedFrom ||
    advCreatedTo ||
    myTagFilter ||
    readingFilter ||
    mode === 'semantic'
  );

  const clearAdv = () => {
    setAdvAuthor('');
    setAdvAffiliation('');
    setAdvPubFrom('');
    setAdvPubTo('');
    setAdvCreatedFrom('');
    setAdvCreatedTo('');
    setMyTagFilter('');
    setReadingFilter('');
    setMode('keyword');
  };

  // 点击作者/机构 → 只留匹配的论文；其余条件重置，面板展开让用户看到生效的条件
  const applyAdvFilter = useCallback(
    (patch: { author?: string; affiliation?: string }) => {
      setMode('keyword');
      setQInput('');
      setAdvAuthor(patch.author ?? '');
      setAdvAffiliation(patch.affiliation ?? '');
      setAdvPubFrom('');
      setAdvPubTo('');
      setAdvCreatedFrom('');
      setAdvCreatedTo('');
      setAdvOpen(true);
      onSelect('');
      if (patch.author) toast(tr(`只看 ${patch.author} 的论文`, `Showing papers by ${patch.author}`), 'info');
      else if (patch.affiliation) toast(tr(`只看 ${patch.affiliation} 的论文`, `Showing papers from ${patch.affiliation}`), 'info');
    },
    [onSelect],
  );

  const filterByAuthor = useCallback((name: string) => applyAdvFilter({ author: name }), [applyAdvFilter]);
  const filterByAffiliation = useCallback((name: string) => applyAdvFilter({ affiliation: name }), [applyAdvFilter]);

  const seedSeq = advSeed?.seq ?? 0;
  useEffect(() => {
    if (!advSeed || advSeed.seq === 0) return;
    applyAdvFilter({ author: advSeed.author, affiliation: advSeed.affiliation });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedSeq]);

  // 多选（批量删除 / 导出 / 对比）
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [compareOpen, setCompareOpen] = useState(false);
  const queryClient = useQueryClient();

  useEffect(() => {
    setSelected(new Set());
    setSelectMode(false);
  }, [scopeId, view, q, myTagFilter, readingFilter]);

  // 生成解读：同步等着（约 1 分钟），按 paper id 记在列表页这一层
  const compilePending = usePendingByPaper();
  const recompile = useCallback(
    (paperId: string) => {
      void compilePending.run(paperId, async () => {
        try {
          await api.recompilePaper(paperId);
          toast(tr('解读已更新', 'Summary updated'), 'ok');
          void queryClient.invalidateQueries({ queryKey: ['paper', scopeId, paperId] });
          void queryClient.invalidateQueries({ queryKey: ['paper-figures', paperId] });
          void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
        } catch (e) {
          toast(`${tr('无法生成解读：', 'Couldn’t generate the summary: ')}${errorText(e)}`, 'error');
        }
      });
    },
    [compilePending, queryClient, scopeId],
  );

  const bulkDeleteMutation = useMutation({
    mutationFn: () =>
      libraryId ? api.batchDeleteLibraryPapers(libraryId, [...selected]) : api.batchDeletePapers(scopeId, [...selected]),
    onSuccess: (res) => {
      toast(tr(`已将 ${res.deleted} 篇移入回收站`, `Moved ${res.deleted} papers to trash`), 'ok');
      if (selectedId && selected.has(selectedId)) onSelect('');
      setSelected(new Set());
      setSelectMode(false);
      void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['project-graph', scopeId] });
    },
    onError: (e) => toast(`${tr('无法删除：', 'Couldn’t delete: ')}${errorText(e)}`, 'error'),
  });

  const bulkExportMutation = useMutation({
    mutationFn: (format: CitationFormat) =>
      libraryId
        ? api.downloadLibraryCitations(libraryId, { format, ids: [...selected] })
        : api.downloadCitations(pid ?? '', { format, ids: [...selected] }),
    onSuccess: (blob, format) => {
      saveBlob(blob, format === 'bibtex' ? 'polaris-selected.bib' : 'polaris-selected.json');
      toast(tr(`已导出 ${selected.size} 篇`, `Exported ${selected.size} papers`), 'ok');
    },
    onError: (e) => toast(`${tr('无法导出：', 'Couldn’t export: ')}${errorText(e)}`, 'error'),
  });

  const extensionBatchMutation = useMutation({
    mutationFn: (targets: PaperRead[]) => {
      if (!libraryId) throw new Error('LIBRARY_REQUIRED');
      return sendLibraryPapersToExtension(libraryId, targets);
    },
    onSuccess: ({ batch, acknowledged, dispatchedCount }) => {
      const skipped = batch.items.filter((item) => item.status === 'skipped').length;
      if (dispatchedCount === 0) {
        toast(tr('所选论文都已有 PDF，无需下载', 'All selected papers already have PDFs'), 'ok');
      } else if (acknowledged) {
        toast(
          tr(
            `已发送 ${dispatchedCount} 篇到浏览器扩展${skipped ? `，${skipped} 篇已有 PDF` : ''}`,
            `Sent ${dispatchedCount} papers to the browser extension${skipped ? `. ${skipped} already had PDFs.` : ''}`,
          ),
          'ok',
        );
      } else {
        toast(tr('已保存，浏览器扩展连接后会领取', 'Saved. The browser extension will pick it up when it connects.'), 'info');
      }
      setSelected(new Set());
      void queryClient.invalidateQueries({ queryKey: ['download-batches', libraryId] });
    },
    onError: (error) =>
      toast(`${tr('无法发送到浏览器扩展：', 'Couldn’t send to the browser extension: ')}${errorText(error)}`, 'error'),
  });

  const semanticActive = mode === 'semantic' && q.length > 0;

  const myTagsQuery = useQuery({ queryKey: ['my-tags'], queryFn: () => api.listMyTags(), retry: false });
  const myTags = myTagsQuery.data ?? [];

  const listQuery = useInfiniteQuery({
    queryKey: ['papers', scopeId, view, q, sort, myTagFilter, readingFilter, author, affiliation, advPubFrom, advPubTo, advCreatedFrom, advCreatedTo],
    queryFn: ({ pageParam }) => {
      const vq = viewQuery(view);
      const opts = {
        ...vq,
        q: q || undefined,
        sort,
        my_tag: myTagFilter || undefined,
        reading_status: readingFilter || undefined,
        author: author || undefined,
        affiliation: affiliation || undefined,
        published_from: advPubFrom ? `${advPubFrom}T00:00:00Z` : undefined,
        published_to: advPubTo ? `${advPubTo}T23:59:59Z` : undefined,
        created_from: advCreatedFrom ? `${advCreatedFrom}T00:00:00Z` : vq.created_from,
        created_to: advCreatedTo ? `${advCreatedTo}T23:59:59Z` : undefined,
        page: pageParam,
        size: PAGE_SIZE,
      };
      return libraryId ? api.listLibraryPapersFull(libraryId, opts) : api.listPapers(scopeId, opts);
    },
    initialPageParam: 1,
    getNextPageParam: (last) => (last.page * last.size < last.total ? last.page + 1 : undefined),
    retry: false,
    enabled: !semanticActive,
  });

  const semQuery = useQuery({
    queryKey: ['wiki-search', scopeId, q],
    queryFn: () =>
      libraryId
        ? api.searchLibrary(libraryId, { q, mode: 'semantic', limit: 30 })
        : api.searchProject(scopeId, { q, mode: 'semantic', limit: 30 }),
    retry: (count, e) => !(e instanceof ApiError) && count < 1,
    enabled: semanticActive,
  });

  const papers: PaperRead[] = useMemo(() => {
    if (semanticActive) return semQuery.data?.papers ?? [];
    return listQuery.data?.pages.flatMap((p) => p.items) ?? [];
  }, [semanticActive, semQuery.data, listQuery.data]);
  const totalShown = semanticActive ? papers.length : (listQuery.data?.pages[0]?.total ?? papers.length);

  const isLoading = semanticActive ? semQuery.isLoading : listQuery.isLoading;
  const isError = semanticActive ? semQuery.isError : listQuery.isError;
  const fallbackNotice = semanticActive && semQuery.data && semQuery.data.mode_used === 'keyword';
  const hasFilter = !!q || view !== 'all' || advActive;

  // 宽屏：列表变化后自动选中第一篇。窄屏一次只看一块，不替人选。
  const firstId = papers[0]?.id ?? null;
  useEffect(() => {
    if (!narrow && !selectedId && firstId) onSelect(firstId);
  }, [narrow, selectedId, firstId, onSelect]);

  return (
    <div className={'split lib-split' + (selectedId ? ' has-selection' : '')}>
      {/* —— 左：列表 —— */}
      <div className="split-list">
        <div className="pl-tools">
          <div className="row gap8">
            <SearchInput
              value={qInput}
              onChange={setQInput}
              placeholder={
                mode === 'semantic'
                  ? tr('用一句话描述要找的内容…', 'Describe what you’re looking for…')
                  : tr('搜索标题、作者或关键词…', 'Search titles, authors or keywords…')
              }
            />
            <button
              type="button"
              className={'icon-btn pl-filter-btn' + (advOpen || advActive ? ' is-on' : '')}
              title={tr('更多筛选', 'More filters')}
              aria-label={tr('更多筛选', 'More filters')}
              aria-expanded={advOpen}
              data-testid="papers-more-filters"
              onClick={() => setAdvOpen((o) => !o)}
            >
              <Icon name="sliders" size={14} />
              {advActive && <span className="pl-filter-dot" />}
            </button>
          </div>
          {advOpen && (
            <div className="pl-adv" data-testid="papers-filters">
              <label className="pl-adv-row">
                <span>{tr('搜索方式', 'Match')}</span>
                <select className="input" value={mode} onChange={(e) => setMode(e.target.value as SearchMode)}>
                  <option value="keyword">{tr('按字面', 'Exact words')}</option>
                  <option value="semantic">{tr('按意思', 'By meaning')}</option>
                </select>
              </label>
              <label className="pl-adv-row">
                <span>{tr('作者', 'Author')}</span>
                <input className="input" value={advAuthor} onChange={(e) => setAdvAuthor(e.target.value)} placeholder={tr('例如 Lewis', 'e.g. Lewis')} />
              </label>
              <label className="pl-adv-row">
                <span>{tr('机构', 'Affiliation')}</span>
                <input className="input" value={advAffiliation} onChange={(e) => setAdvAffiliation(e.target.value)} placeholder={tr('例如 Tsinghua', 'e.g. Tsinghua')} />
              </label>
              <div className="pl-adv-row">
                <span>{tr('发表时间', 'Published')}</span>
                <input className="input" type="date" value={advPubFrom} onChange={(e) => setAdvPubFrom(e.target.value)} aria-label={tr('发表时间起', 'Published from')} />
                <span className="pl-adv-dash">–</span>
                <input className="input" type="date" value={advPubTo} onChange={(e) => setAdvPubTo(e.target.value)} aria-label={tr('发表时间止', 'Published to')} />
              </div>
              <div className="pl-adv-row">
                <span>{tr('加入时间', 'Added')}</span>
                <input className="input" type="date" value={advCreatedFrom} onChange={(e) => setAdvCreatedFrom(e.target.value)} aria-label={tr('加入时间起', 'Added from')} />
                <span className="pl-adv-dash">–</span>
                <input className="input" type="date" value={advCreatedTo} onChange={(e) => setAdvCreatedTo(e.target.value)} aria-label={tr('加入时间止', 'Added to')} />
              </div>
              <label className="pl-adv-row">
                <span>{tr('我的标签', 'My tag')}</span>
                <select className="input" value={myTagFilter} onChange={(e) => setMyTagFilter(e.target.value)}>
                  <option value="">{tr('不限', 'Any')}</option>
                  {myTags.map((t) => (
                    <option key={t.name} value={t.name}>
                      {t.name}（{t.paper_count}）
                    </option>
                  ))}
                </select>
              </label>
              <label className="pl-adv-row">
                <span>{tr('阅读状态', 'Reading')}</span>
                <select className="input" value={readingFilter} onChange={(e) => setReadingFilter(e.target.value as '' | ReadingStatus)}>
                  <option value="">{tr('不限', 'Any')}</option>
                  {READING_STATUS.map((m) => (
                    <option key={m.v} value={m.v}>
                      {tr(m.label, m.en)}
                    </option>
                  ))}
                </select>
              </label>
              {advActive && (
                <button type="button" className="btn btn-plain xs" style={{ alignSelf: 'flex-start' }} onClick={clearAdv}>
                  {tr('清除筛选', 'Clear filters')}
                </button>
              )}
            </div>
          )}
          <div className="pl-quiet" style={semanticActive ? { opacity: 0.45, pointerEvents: 'none' } : undefined}>
            <div className="pl-quiet-tabs" role="tablist">
              {VIEW_FILTERS.map((f) => (
                <button
                  key={f.v}
                  type="button"
                  role="tab"
                  aria-selected={view === f.v}
                  className={'pl-qtab' + (view === f.v ? ' is-on' : '')}
                  title={tr(f.hintZh, f.hintEn)}
                  onClick={() => setView(f.v)}
                >
                  {tr(f.zh, f.en)}
                </button>
              ))}
            </div>
            <select className="pl-sort" value={sort} onChange={(e) => setSort(e.target.value as PaperSort)} aria-label={tr('排序', 'Sort')} data-testid="papers-sort">
              {SORTS.map((s) => (
                <option key={s.v} value={s.v}>
                  {tr(s.zh, s.en)}
                </option>
              ))}
            </select>
          </div>
          {fallbackNotice && (
            <div className="hint" style={{ marginTop: 6, color: 'var(--warn-tx)' }}>
              {tr('按意思搜暂时不可用，已按字面搜', 'Search by meaning isn’t available, so exact words were used')}
            </div>
          )}
        </div>

        <div className="scroll" style={{ overflowY: 'auto', flex: 1 }} data-testid="papers-list">
          {isLoading ? (
            <LoadingState rows={5} />
          ) : isError ? (
            <EmptyState
              compact
              icon="x"
              title={tr('无法加载论文', 'Couldn’t load papers')}
              desc={tr('请确认本机引擎正在运行。', 'Check that the local engine is running.')}
            />
          ) : papers.length === 0 ? (
            <EmptyState
              compact
              icon="book"
              title={
                hasFilter
                  ? view === 'today' && !q && !advActive
                    ? tr('上次更新没有新加入的论文', 'The last update added no papers')
                    : tr('没有匹配的论文', 'No matching papers')
                  : tr('还没有论文', 'No papers yet')
              }
              action={
                !hasFilter && onFindPapers ? (
                  <button type="button" className="btn btn-soft sm" onClick={onFindPapers}>
                    <Icon name="search" size={13} />
                    {tr('找论文', 'Find papers')}
                  </button>
                ) : undefined
              }
            />
          ) : (
            <>
              {papers.map((p) => (
                <LibraryPaperRow
                  key={p.id}
                  p={p}
                  active={p.id === selectedId}
                  checked={selected.has(p.id)}
                  selectMode={selectMode}
                  onClick={() => onSelect(p.id)}
                  onToggleCheck={() =>
                    setSelected((old) => {
                      const next = new Set(old);
                      if (next.has(p.id)) next.delete(p.id);
                      else next.add(p.id);
                      return next;
                    })
                  }
                />
              ))}
              {!semanticActive && listQuery.hasNextPage && (
                <div style={{ padding: 12, display: 'flex', justifyContent: 'center' }}>
                  <button className="btn btn-plain sm" disabled={listQuery.isFetchingNextPage} onClick={() => void listQuery.fetchNextPage()}>
                    {listQuery.isFetchingNextPage ? tr('加载中…', 'Loading…') : tr('加载更多', 'Load more')}
                  </button>
                </div>
              )}
            </>
          )}
        </div>

        {/* —— 底栏：数量 + 多选 —— */}
        <div className="pl-foot">
          {selectMode ? (
            <>
              <span>{tr(`已选 ${selected.size} 篇`, `${selected.size} selected`)}</span>
              <button
                className="btn btn-danger-soft xs"
                disabled={selected.size === 0 || bulkDeleteMutation.isPending}
                onClick={() => bulkDeleteMutation.mutate()}
              >
                {tr('移入回收站', 'Move to trash')}
              </button>
              <ExportDropdown
                sm
                placement="up"
                align="left"
                busy={bulkExportMutation.isPending}
                disabled={selected.size === 0}
                items={citationExportItems((format) => bulkExportMutation.mutate(format))}
              />
              {libraryId && selected.size >= 2 && (
                <button
                  className="btn btn-ghost xs"
                  disabled={selected.size > 10}
                  title={
                    selected.size > 10
                      ? tr('一次最多对比 10 篇', 'Compare at most 10 papers at once')
                      : tr('并排对比所选论文', 'Compare the selected papers side by side')
                  }
                  onClick={() => setCompareOpen(true)}
                >
                  {tr('对比', 'Compare')}
                </button>
              )}
              {libraryId && extensionDeliveryAvailable() && (
                <button
                  className="btn btn-ghost xs"
                  disabled={selected.size === 0 || extensionBatchMutation.isPending}
                  onClick={() => extensionBatchMutation.mutate(papers.filter((paper) => selected.has(paper.id)))}
                >
                  {tr('用浏览器扩展下载 PDF', 'Get PDFs via the extension')}
                </button>
              )}
              <button
                className="btn btn-plain xs"
                style={{ marginLeft: 'auto' }}
                onClick={() => {
                  setSelectMode(false);
                  setSelected(new Set());
                }}
              >
                {tr('完成', 'Done')}
              </button>
            </>
          ) : (
            <>
              <span data-testid="papers-count">
                {hasFilter
                  ? tr(`${totalShown} 篇符合`, `${totalShown} ${totalShown === 1 ? 'match' : 'matches'}`)
                  : tr(`共 ${totalShown} 篇`, `${totalShown} ${totalShown === 1 ? 'paper' : 'papers'}`)}
              </span>
              <button className="btn btn-plain xs" style={{ marginLeft: 'auto' }} onClick={() => setSelectMode(true)} disabled={papers.length === 0}>
                <Icon name="check" size={12} />
                {tr('多选', 'Select')}
              </button>
            </>
          )}
        </div>
      </div>

      {/* —— 右：详情 —— */}
      <div className="split-detail">
        {selectedId ? (
          <PaperDetailPane
            paperId={selectedId}
            pid={pid ?? ''}
            libraryId={libraryId}
            onOpenConcept={onOpenConcept}
            onWikiLink={onWikiLink}
            onFilterAuthor={filterByAuthor}
            onFilterAffiliation={filterByAffiliation}
            onDeleted={() => onSelect('')}
            onBack={() => onSelect('')}
            compiling={compilePending.has(selectedId)}
            onRecompile={recompile}
            sendingToExtension={extensionBatchMutation.isPending}
            onSendToExtension={(paper) => extensionBatchMutation.mutate([paper])}
          />
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            {tr('从列表中选择一篇论文', 'Select a paper from the list')}
          </div>
        )}
      </div>

      {libraryId && (
        <ComparisonModal libraryId={libraryId} paperIds={[...selected]} open={compareOpen} onClose={() => setCompareOpen(false)} />
      )}
    </div>
  );
}
