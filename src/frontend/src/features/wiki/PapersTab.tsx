import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PaperStatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { ScoreRing } from '../../components/ui/ScoreRing';
import { EmptyState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { FigureEmbed, FiguresSection, hasEmbeddedFigures, usePaperFigures } from '../../components/ui/FigureGallery';
import { CompileBadge } from '../../components/ui/CompileBadge';
import { PaperReader } from './PaperReader';
import { readerFrom } from '../reading/shared';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import {
  api,
  ApiError,
  type CitationFormat,
  type MyMeta,
  type PaperDetail,
  type PaperImportInput,
  type PaperRead,
  type PaperSort,
  type PaperStatusFilter,
  type ReadingStatus,
  type SearchMode,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { clickable } from '../../lib/a11y';
import { categoryMeta, saveBlob, SearchInput, useDebounced } from './shared';
import { READING_STATUS, ReadingDot } from '../reading/shared';
import { AddToButton } from '../library/AddToPopover';
import { PaperProgressModal } from '../library/PaperProgressModal';

/* ============================================================
   论文库 Tab：左列表（过滤/搜索/排序/加载更多 + 添加文献/导出）
   + 右详情（元数据 + wiki markdown + 概念 chips + 标签/星标/
   阅读状态 + 编译/删除 + 阅读入口）；列表支持多选批量删除/导出。
   ============================================================ */

const PAGE_SIZE = 20;

/** 论文库视图（docs/api-lit.md §8.5）：全部 = 已纳入（相关性达标）的文献；
    相关性不足的进垃圾桶，不显示不计数。 */
type ViewFilter = 'all' | 'compiled' | 'starred';

// 模块级常量存 zh/en 两份文案，渲染处再 tr（import 时求值不会随语言切换更新）
const VIEW_FILTERS: { v: ViewFilter; zh: string; en: string; hintZh: string; hintEn: string }[] = [
  { v: 'all', zh: '全部', en: 'All', hintZh: '已纳入知识库的全部文献', hintEn: 'Every paper included in the library' },
  { v: 'compiled', zh: '已编译', en: 'Compiled', hintZh: 'AI 已精读编译出介绍', hintEn: 'Papers the AI has compiled an intro for' },
  { v: 'starred', zh: '已星标', en: 'Starred', hintZh: '我加了星标的文献', hintEn: 'Papers I starred' },
];

/** 视图 → 列表查询参数（未纳入/垃圾桶文献一律不出现在论文库）。 */
function viewQuery(view: ViewFilter): { status: PaperStatusFilter; starred?: boolean } {
  if (view === 'compiled') return { status: 'compiled_any' };
  if (view === 'starred') return { status: 'library', starred: true };
  return { status: 'library' };
}

/** 深链 /wiki?author= / ?affiliation= 带进来的高级检索条件；seq 递增触发重新应用 */
export interface AdvSearchSeed {
  author?: string;
  affiliation?: string;
  seq: number;
}

export interface PapersTabProps {
  pid?: string;
  /** 独立库作用域：给定时集合级调用走 /libraries/{id}/* 端点，并隐藏标签编辑/过滤 */
  libraryId?: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onOpenConcept: (id: string) => void;
  /** wiki 双链 [[概念名]] 点击 → 按名称跳概念 */
  onWikiLink: WikiLinkHandler;
  /** 深链带入的作者/机构筛选（阅读页跳回文献库用） */
  advSeed?: AdvSearchSeed | null;
}

/* ---------------- 添加文献 Modal ---------------- */

type ImportMethod = 'arxiv' | 'doi' | 'bibtex';

function AddPaperModal({
  pid,
  libraryId,
  open,
  onClose,
  onImported,
}: {
  pid: string;
  libraryId?: string;
  open: boolean;
  onClose: () => void;
  /** 添加成功 / 已存在时跳转选中该论文 */
  onImported: (paperId: string) => void;
}) {
  const queryClient = useQueryClient();
  const scopeId = libraryId ?? pid;
  const [method, setMethod] = useState<ImportMethod>('arxiv');
  const [arxivId, setArxivId] = useState('');
  const [doi, setDoi] = useState('');
  const [bibtex, setBibtex] = useState('');
  const [parseError, setParseError] = useState<string | null>(null);
  // 手动添加后若后端返回 task_id，弹出分阶段处理进度
  const [progress, setProgress] = useState<{ taskId: string; title: string } | null>(null);

  const invalidateLists = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
    void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
  }, [queryClient, scopeId]);

  const input: PaperImportInput | null =
    method === 'arxiv'
      ? arxivId.trim()
        ? { arxiv_id: arxivId.trim() }
        : null
      : method === 'doi'
        ? doi.trim()
          ? { doi: doi.trim() }
          : null
        : bibtex.trim()
          ? { bibtex: bibtex.trim() }
          : null;

  const reset = () => {
    setArxivId('');
    setDoi('');
    setBibtex('');
    setParseError(null);
  };

  const importMutation = useMutation({
    mutationFn: (inp: PaperImportInput) => (libraryId ? api.importLibraryPaper(libraryId, inp) : api.importPaper(pid, inp)),
    onSuccess: (p) => {
      invalidateLists();
      reset();
      onClose();
      onImported(p.id);
      if (p.task_id) {
        // 还需后处理：弹进度弹窗替代成功 toast，避免重复打扰
        setProgress({ taskId: p.task_id, title: p.title });
      } else {
        toast(tr('文献已加进论文库', 'Paper added to the library'), 'ok');
      }
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        const paperId = (e.body as { paper_id?: string } | null | undefined)?.paper_id;
        toast(tr('这篇论文已经在库中，已为你打开', 'This paper is already in the library — opened it for you'), 'info');
        reset();
        onClose();
        if (paperId) onImported(paperId);
      } else if (e instanceof ApiError && e.status === 422) {
        setParseError(
          e.message.replace(/^PARSE_FAILED:?\s*/, '') || tr('内容解析失败，请检查格式', 'Failed to parse — check the format'),
        );
      } else {
        toast(`${tr('添加失败：', 'Failed to add: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  return (
    <>
    <Modal
      open={open}
      onClose={onClose}
      title={tr('添加文献', 'Add paper')}
      sub={libraryId ? tr('手动把一篇论文加进这个文献库', 'Manually add a paper to this library') : tr('手动把一篇论文加进当前课题的论文库', 'Manually add a paper to this topic’s library')}
      width={520}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>
            {tr('取消', 'Cancel')}
          </button>
          <button
            className="btn btn-primary sm"
            disabled={!input || importMutation.isPending}
            onClick={() => input && importMutation.mutate(input)}
          >
            {importMutation.isPending ? (
              <>
                <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('添加中…', 'Adding…')}
              </>
            ) : (
              <>
                <Icon name="plus" size={13} />
                {tr('添加', 'Add')}
              </>
            )}
          </button>
        </>
      }
    >
      <Segmented<ImportMethod>
        options={[
          { v: 'arxiv', label: 'arXiv ID' },
          { v: 'doi', label: 'DOI' },
          { v: 'bibtex', label: tr('BibTeX 粘贴', 'Paste BibTeX') },
        ]}
        value={method}
        onChange={(m) => {
          setMethod(m);
          setParseError(null);
        }}
      />
      <div style={{ marginTop: 14 }}>
        {method === 'arxiv' ? (
          <>
            <input
              className="input mono"
              style={{ width: '100%' }}
              placeholder={tr('例如 2405.01234 或 2405.01234v2', 'e.g. 2405.01234 or 2405.01234v2')}
              value={arxivId}
              onChange={(e) => {
                setArxivId(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              {tr(
                '填 arXiv 编号即可，标题、作者、摘要会自动抓取，并顺带下载 PDF。',
                'Just the arXiv ID — title, authors, and abstract are fetched automatically, plus the PDF.',
              )}
            </div>
          </>
        ) : method === 'doi' ? (
          <>
            <input
              className="input mono"
              style={{ width: '100%' }}
              placeholder={tr('例如 10.1145/3567890.1234567', 'e.g. 10.1145/3567890.1234567')}
              value={doi}
              onChange={(e) => {
                setDoi(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              {tr(
                '通过 DOI 反查论文信息（OpenAlex），适合期刊/会议论文。',
                'Looks up paper metadata by DOI (OpenAlex) — good for journal/conference papers.',
              )}
            </div>
          </>
        ) : (
          <>
            <textarea
              className="textarea mono"
              style={{ width: '100%', minHeight: 150, resize: 'vertical', fontSize: 12 }}
              placeholder={tr(
                '粘贴单条 BibTeX 条目，例如：\n@inproceedings{smith2024example,\n  title = {...},\n  author = {...},\n  year = {2024},\n}',
                'Paste one BibTeX entry, e.g.:\n@inproceedings{smith2024example,\n  title = {...},\n  author = {...},\n  year = {2024},\n}',
              )}
              value={bibtex}
              onChange={(e) => {
                setBibtex(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              {tr(
                '一次粘贴一条；title 必填，作者/年份/期刊/DOI 能解析多少取多少。',
                'One entry at a time; title is required — authors/year/venue/DOI are parsed on a best-effort basis.',
              )}
            </div>
          </>
        )}
        {parseError && (
          <div
            style={{
              marginTop: 10,
              fontSize: 11.5,
              color: 'var(--danger-tx)',
              background: 'var(--danger-bg)',
              borderRadius: 8,
              padding: '7px 10px',
              lineHeight: 1.6,
            }}
          >
            {tr('解析失败：', 'Parse failed: ')}{parseError}
          </div>
        )}
      </div>
    </Modal>
    {progress && (
      <PaperProgressModal
        taskId={progress.taskId}
        paperTitle={progress.title}
        onClose={() => setProgress(null)}
        onDone={invalidateLists}
      />
    )}
    </>
  );
}

/* ---------------- 导出下拉菜单 ---------------- */

export function ExportMenu({
  pid,
  filters = {},
}: {
  pid: string;
  /** 列表过滤条件，透传给引用导出；不传导出全部库内文献 */
  filters?: { status?: PaperStatusFilter; tag?: string; starred?: boolean };
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // 点外面关闭
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const obsidianMutation = useMutation({
    mutationFn: () => api.downloadObsidianExport(pid),
    onSuccess: (blob) => {
      saveBlob(blob, 'polaris-wiki.zip');
      toast(tr('Obsidian 笔记库已导出', 'Obsidian vault exported'), 'ok');
    },
    onError: (e) =>
      toast(`${tr('导出失败：', 'Export failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const citationsMutation = useMutation({
    mutationFn: (format: CitationFormat) => api.downloadCitations(pid, { format, ...filters }),
    onSuccess: (blob, format) => {
      saveBlob(blob, format === 'bibtex' ? 'polaris-references.bib' : 'polaris-references.json');
      toast(
        format === 'bibtex' ? tr('BibTeX 文件已导出', 'BibTeX file exported') : tr('CSL-JSON 文件已导出', 'CSL-JSON file exported'),
        'ok',
      );
    },
    onError: (e) =>
      toast(`${tr('导出失败：', 'Export failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const busy = obsidianMutation.isPending || citationsMutation.isPending;

  const itemStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    width: '100%',
    padding: '8px 12px',
    border: 'none',
    background: 'transparent',
    cursor: 'pointer',
    fontSize: 12,
    fontFamily: 'var(--sans)',
    color: 'var(--text)',
    textAlign: 'left',
  };

  return (
    <div ref={wrapRef} style={{ position: 'relative' }}>
      <button className="btn btn-ghost" onClick={() => setOpen((o) => !o)} disabled={busy}>
        {busy ? (
          <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
        ) : (
          <Icon name="download" size={14} />
        )}
        {tr('导出', 'Export')}
        <Icon name="chevDown" size={12} />
      </button>
      {open && (
        <div
          className="card"
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            right: 0,
            zIndex: 30,
            minWidth: 210,
            padding: '4px 0',
            boxShadow: 'var(--shadow-pop)',
          }}
        >
          <button
            style={itemStyle}
            onClick={() => {
              setOpen(false);
              obsidianMutation.mutate();
            }}
          >
            <Icon name="file" size={13} style={{ color: 'var(--text-3)' }} />
            <span>
              {tr('Obsidian 笔记库', 'Obsidian vault')}
              <span className="muted" style={{ marginLeft: 5, fontSize: 10.5 }}>.zip</span>
            </span>
          </button>
          <button
            style={itemStyle}
            onClick={() => {
              setOpen(false);
              citationsMutation.mutate('bibtex');
            }}
          >
            <Icon name="book" size={13} style={{ color: 'var(--text-3)' }} />
            <span>
              {tr('BibTeX 引用', 'BibTeX citations')}
              <span className="muted" style={{ marginLeft: 5, fontSize: 10.5 }}>
                {tr('.bib · 全部库内文献', '.bib · whole library')}
              </span>
            </span>
          </button>
          <button
            style={itemStyle}
            onClick={() => {
              setOpen(false);
              citationsMutation.mutate('csl-json');
            }}
          >
            <Icon name="layers" size={13} style={{ color: 'var(--text-3)' }} />
            <span>
              CSL-JSON
              <span className="muted" style={{ marginLeft: 5, fontSize: 10.5 }}>
                {tr('Zotero 可直接导入', 'imports straight into Zotero')}
              </span>
            </span>
          </button>
        </div>
      )}
    </div>
  );
}

/* ---------------- 垃圾桶 ---------------- */

function TrashModal({ pid, libraryId, open, onClose }: { pid: string; libraryId?: string; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const scopeId = libraryId ?? pid;
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const [trashQ, setTrashQ] = useState('');

  const trashQuery = useQuery({
    queryKey: ['papers-trash', scopeId],
    queryFn: () =>
      libraryId
        ? api.listLibraryPapersFull(libraryId, { status: 'excluded', size: 100, sort: '-published_at' })
        : api.listPapers(pid, { status: 'excluded', size: 100, sort: '-published_at' }),
    enabled: open,
    retry: false,
  });
  const allItems = trashQuery.data?.items ?? [];
  // 桶内搜索：标题 / 作者（客户端过滤）
  const kw = trashQ.trim().toLowerCase();
  const items = kw
    ? allItems.filter(
        (p) =>
          p.title.toLowerCase().includes(kw) ||
          p.authors.some((a) => a.name.toLowerCase().includes(kw)),
      )
    : allItems;

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
      toast(`${tr('已召回：', 'Restored: ')}${p.title.slice(0, 30)}`, 'ok');
      invalidate();
    },
    onError: (e) =>
      toast(`${tr('召回失败：', 'Restore failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
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
      toast(`${tr('删除失败：', 'Delete failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const emptyMutation = useMutation({
    mutationFn: () => (libraryId ? api.emptyLibraryTrash(libraryId) : api.emptyTrash(pid)),
    onSuccess: (res) => {
      toast(tr(`垃圾桶已清空（${res.deleted} 篇）`, `Trash emptied (${res.deleted} papers)`), 'ok');
      setConfirmEmpty(false);
      invalidate();
    },
    onError: (e) =>
      toast(`${tr('清空失败：', 'Empty failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const busy = restoreMutation.isPending || purgeMutation.isPending || emptyMutation.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr('垃圾桶', 'Trash')}
      sub={tr(
        '相关性不足自动淘汰与手动删除的文献；召回后回到论文库',
        'Papers auto-dropped for low relevance or deleted manually; restoring puts them back in the library',
      )}
      width={680}
      footer={
        <>
          {confirmEmpty ? (
            <>
              <span style={{ fontSize: 12, color: 'var(--danger-tx)', marginRight: 'auto' }}>
                {tr(
                  `将彻底删除全部 ${allItems.length} 篇及其文件，无法恢复`,
                  `This permanently deletes all ${allItems.length} papers and their files — no undo`,
                )}
              </span>
              <button className="btn btn-ghost sm" onClick={() => setConfirmEmpty(false)}>
                {tr('取消', 'Cancel')}
              </button>
              <button
                className="btn btn-primary sm"
                style={{ background: 'var(--danger-tx)' }}
                disabled={busy}
                onClick={() => emptyMutation.mutate()}
              >
                {emptyMutation.isPending ? tr('清空中…', 'Emptying…') : tr('确认清空', 'Confirm empty')}
              </button>
            </>
          ) : (
            <>
              <button
                className="btn btn-ghost sm"
                style={{ color: 'var(--danger-tx)', marginRight: 'auto' }}
                disabled={allItems.length === 0 || busy}
                onClick={() => setConfirmEmpty(true)}
              >
                <Icon name="x" size={12} />
                {tr('清空垃圾桶', 'Empty trash')}
              </button>
              <button className="btn btn-soft sm" onClick={onClose}>
                {tr('关闭', 'Close')}
              </button>
            </>
          )}
        </>
      }
    >
      {/* 搜索区固定不随列表滚动：列表自带滚动容器，整体高度不超出 Modal 内容区 */}
      <div className="row gap10" style={{ marginBottom: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <SearchInput value={trashQ} onChange={setTrashQ} placeholder={tr('搜索标题 / 作者…', 'Search title / author…')} />
        </div>
        <span className="mono muted" style={{ fontSize: 11, flexShrink: 0 }}>
          {kw
            ? tr(`${items.length} / ${allItems.length} 篇`, `${items.length} / ${allItems.length}`)
            : tr(`${allItems.length} 篇`, `${allItems.length} papers`)}
        </span>
      </div>
      {trashQuery.isLoading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : allItems.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>{tr('垃圾桶是空的', 'Trash is empty')}</div>
      ) : items.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>{tr('没有匹配的文献', 'No matching papers')}</div>
      ) : (
        <div
          className="scroll"
          style={{
            maxHeight: '46vh',
            overflowY: 'auto',
            border: '0.5px solid var(--border)',
            borderRadius: 10,
          }}
        >
          {items.map((p, i) => (
            <TrashRow key={p.id} p={p} last={i === items.length - 1} busy={busy}
              onRestore={() => restoreMutation.mutate(p.id)}
              onPurge={() => purgeMutation.mutate(p.id)}
            />
          ))}
          {(trashQuery.data?.total ?? 0) > allItems.length && (
            <div className="muted" style={{ fontSize: 11, textAlign: 'center', padding: 8 }}>
              {tr(
                `仅显示最近 ${allItems.length} 篇（共 ${trashQuery.data?.total} 篇）`,
                `Showing the latest ${allItems.length} (of ${trashQuery.data?.total})`,
              )}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

/** 垃圾桶原因标签：打分淘汰 = 不相关；否则视为手动删除（老数据缺字段时按分数推断）。 */
function trashReasonOf(p: PaperRead): 'irrelevant' | 'manual' {
  if (p.trash_reason === 'manual' || p.trash_reason === 'irrelevant') return p.trash_reason;
  return p.relevance_score !== null ? 'irrelevant' : 'manual';
}

/** 垃圾桶列表行：与论文库 PaperRow 同款版式，标签换成删除原因，右侧召回/彻底删除。 */
function TrashRow({
  p,
  last,
  busy,
  onRestore,
  onPurge,
}: {
  p: PaperRead;
  last: boolean;
  busy: boolean;
  onRestore: () => void;
  onPurge: () => void;
}) {
  const reason = trashReasonOf(p);
  return (
    <div
      className="row gap10"
      style={{
        padding: '12px 16px',
        borderBottom: last ? 'none' : '0.5px solid var(--border)',
        alignItems: 'flex-start',
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row gap8" style={{ marginBottom: 5 }}>
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
            {p.arxiv_id ?? p.venue ?? '—'}
          </span>
          {p.year !== null && (
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
              {p.year}
            </span>
          )}
          {p.has_wiki && <Icon name="sparkle" size={11} style={{ color: 'var(--accent)' }} />}
          <span style={{ marginLeft: 'auto' }}>
            <RelevanceBar value={p.relevance_score} />
          </span>
        </div>
        <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: 'var(--text)' }}>{p.title}</div>
        <div className="row gap8" style={{ marginTop: 6 }}>
          {reason === 'irrelevant' ? (
            <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
              {tr('不相关', 'Irrelevant')}
            </span>
          ) : (
            <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
              {tr('手动删除', 'Deleted manually')}
            </span>
          )}
          {p.tldr && (
            <span
              style={{
                flex: 1,
                minWidth: 0,
                fontSize: 11.5,
                color: 'var(--text-3)',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {p.tldr}
            </span>
          )}
        </div>
      </div>
      <div className="col" style={{ gap: 6, flexShrink: 0 }}>
        <button
          className="btn btn-soft sm"
          style={{ height: 26 }}
          disabled={busy}
          title={tr('召回到论文库', 'Restore to the library')}
          onClick={onRestore}
        >
          <Icon name="refresh" size={12} />
          {tr('召回', 'Restore')}
        </button>
        <button
          className="btn btn-ghost sm"
          style={{ height: 26, color: 'var(--danger-tx)' }}
          disabled={busy}
          title={tr('彻底删除（连同文件，无法恢复）', 'Delete permanently (files included, no undo)')}
          onClick={onPurge}
        >
          <Icon name="x" size={12} />
          {tr('彻底删除', 'Delete forever')}
        </button>
      </div>
    </div>
  );
}

/* ---------------- 列表行 ---------------- */

/* memo：父组件（大量筛选/选中 state）任一变更都会触发全列表重渲染。
   忽略函数 props 的比较是安全的：两个 handler 只捕获稳定引用与 p.id。 */
const PaperRow = memo(function PaperRow({
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
  const tags = p.tags ?? [];
  return (
    <div
      onClick={onClick}
      style={{
        padding: '12px 16px',
        cursor: 'pointer',
        borderBottom: '0.5px solid var(--border)',
        background: active ? 'var(--accent-soft)' : 'transparent',
        borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
        transition: 'background 0.12s',
      }}
    >
      <div className="row gap8" style={{ marginBottom: 5 }}>
        {/* 占位常驻：切换多选时行内容不左右跳（#132） */}
        <input
          type="checkbox"
          checked={checked}
          onClick={(e) => e.stopPropagation()}
          onChange={onToggleCheck}
          title={tr('选中后可批量删除 / 导出', 'Select for bulk delete / export')}
          style={{ width: 13, height: 13, margin: 0, flexShrink: 0, accentColor: 'var(--accent)', cursor: 'pointer', visibility: selectMode ? 'visible' : 'hidden' }}
        />
        {p.starred && <Icon name="starFill" size={11} style={{ color: 'var(--warn-tx)', flexShrink: 0 }} />}
        <span className="mono" style={{ fontSize: 10.5, color: active ? 'var(--accent-text)' : 'var(--text-3)' }}>
          {p.arxiv_id ?? p.venue ?? '—'}
        </span>
        {p.year !== null && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
            {p.year}
          </span>
        )}
        {p.has_wiki && <Icon name="sparkle" size={11} style={{ color: 'var(--accent)' }} />}
        <span style={{ marginLeft: 'auto' }}>
          <RelevanceBar value={p.relevance_score} />
        </span>
      </div>
      <div className="row gap8" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: 'var(--text)' }}>
          {p.title}
        </div>
        <AddToButton paperId={p.id} />
      </div>
      <div className="row gap8" style={{ marginTop: 6 }}>
        <PaperStatusPill status={p.status} sm />
        <ReadingDot status={p.reading_status} />
        {tags.slice(0, 2).map((t) => (
          <span
            key={t}
            className="tag"
            style={{ fontSize: 10, height: 17, padding: '0 6px', maxWidth: 90, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'inline-block', lineHeight: '17px' }}
          >
            {t}
          </span>
        ))}
        {tags.length > 2 && (
          <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>
            +{tags.length - 2}
          </span>
        )}
        {(p.note_count ?? 0) > 0 && (
          <span
            className="row"
            style={{ gap: 3, fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}
            title={tr(`${p.note_count} 条笔记`, `${p.note_count} notes`)}
          >
            <Icon name="pen" size={10} />
            {p.note_count}
          </span>
        )}
        {p.tldr && (
          <span
            style={{
              flex: 1,
              minWidth: 0,
              fontSize: 11.5,
              color: 'var(--text-3)',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {p.tldr}
          </span>
        )}
      </div>
    </div>
  );
}, (prev, next) =>
  prev.p === next.p && prev.active === next.active && prev.checked === next.checked && prev.selectMode === next.selectMode,
);

/* ---------------- 标签就地编辑 ---------------- */

function TagEditor({ paper, scopeId }: { paper: PaperDetail; scopeId: string }) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [value, setValue] = useState('');
  const tags = paper.tags ?? [];

  const putMutation = useMutation({
    mutationFn: (names: string[]) => api.putPaperTags(paper.id, names),
    onSuccess: (p) => {
      queryClient.setQueryData<PaperDetail>(['paper', scopeId, paper.id], p);
      void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['project-tags', scopeId] });
    },
    onError: (e) =>
      toast(`${tr('标签更新失败：', 'Tag update failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const commit = () => {
    const name = value.trim();
    setAdding(false);
    setValue('');
    if (!name || tags.includes(name)) return;
    putMutation.mutate([...tags, name]);
  };

  return (
    <div className="row gap6 wrap" style={{ marginTop: 14 }}>
      <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginRight: 2 }}>
        {tr('标签', 'Tags')}
      </span>
      {tags.map((t) => (
        <span key={t} className="tag" style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          {t}
          <span
            title={tr('移除标签', 'Remove tag')}
            style={{ cursor: 'pointer', display: 'inline-flex', opacity: 0.6 }}
            onClick={() => putMutation.mutate(tags.filter((x) => x !== t))}
          >
            <Icon name="x" size={9} />
          </span>
        </span>
      ))}
      {adding ? (
        <input
          className="input"
          autoFocus
          style={{ height: 24, fontSize: 11.5, width: 120, padding: '0 8px' }}
          placeholder={tr('标签名，回车确定', 'Tag name, Enter to confirm')}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) commit();
            if (e.key === 'Escape') {
              setAdding(false);
              setValue('');
            }
          }}
        />
      ) : (
        <span
          className="chip"
          style={{ fontSize: 11, opacity: putMutation.isPending ? 0.5 : 1 }}
          onClick={() => !putMutation.isPending && setAdding(true)}
        >
          <Icon name="plus" size={10} style={{ display: 'inline-block', verticalAlign: -1 }} /> {tr('标签', 'Tag')}
        </span>
      )}
    </div>
  );
}

/* ---------------- 详情面板 ---------------- */

/** 概念 chips 默认最多展示数，超出折叠 */
const CONCEPT_CHIP_LIMIT = 12;

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

/** 机构 chips 默认最多展示数，超出折叠 */
const AFFIL_CHIP_LIMIT = 6;

function PaperDetailPane({
  paperId,
  pid,
  libraryId,
  onOpenConcept,
  onWikiLink,
  onFilterAuthor,
  onFilterAffiliation,
  onDeleted,
}: {
  paperId: string;
  pid: string;
  libraryId?: string;
  onOpenConcept: (id: string) => void;
  onWikiLink: WikiLinkHandler;
  /** 点击作者名 → 论文库按该作者过滤 */
  onFilterAuthor: (name: string) => void;
  /** 点击机构 → 论文库按该机构过滤 */
  onFilterAffiliation: (name: string) => void;
  /** 删除成功后回调（父组件清空选中，自动跳到列表第一篇） */
  onDeleted: () => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const scopeId = libraryId ?? pid;
  const [abstractOpen, setAbstractOpen] = useState(false);
  const [conceptsOpen, setConceptsOpen] = useState(false);
  const [affilsOpen, setAffilsOpen] = useState(false);
  const [readerOpen, setReaderOpen] = useState(false);
  const [readerPrint, setReaderPrint] = useState(false);

  // 作用域读：锁定当前库/课题那份成员行，避免同一论文属多个库时读到跨库归并的错行
  // （相关度/状态/wiki）。queryKey 带 scope 隔离不同库的缓存。
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
    // 作用域删：只删当前库那份成员行（同列表多选删除口径），不误删跨库的另一份。
    mutationFn: () =>
      libraryId
        ? api.batchDeleteLibraryPapers(libraryId, [paperId])
        : api.batchDeletePapers(scopeId, [paperId]),
    onSuccess: () => {
      toast(tr('已移入垃圾桶，可在列表底部的垃圾桶中召回', 'Moved to trash — restore it from the trash any time'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['paper', scopeId, paperId] });
      void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['papers-trash', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['project-graph', scopeId] });
      onDeleted();
    },
    onError: (e) =>
      toast(`${tr('删除失败：', 'Delete failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // 星标 / 阅读状态（个人视角）
  const metaMutation = useMutation({
    mutationFn: (input: Partial<MyMeta>) => api.putMyMeta(paperId, input),
    onSuccess: (meta) => {
      queryClient.setQueryData<PaperDetail>(['paper', scopeId, paperId], (old) =>
        old ? { ...old, starred: meta.starred, reading_status: meta.reading_status } : old,
      );
      void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
    },
    onError: (e) =>
      toast(`${tr('更新失败：', 'Update failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // 重新编译：用最新的图文模式重写 wiki 页（同步调用，约 1 分钟）
  const recompileMutation = useMutation({
    mutationFn: () => api.recompilePaper(paperId),
    onSuccess: () => {
      toast(tr('编译完成，介绍已更新', 'Compiled — the intro has been updated'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['paper', scopeId, paperId] });
      void queryClient.invalidateQueries({ queryKey: ['paper-figures', paperId] });
      void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
    },
    onError: (e) =>
      toast(`${tr('重新编译失败：', 'Recompile failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // 正文 ![[fig:N]] 嵌入图（docs/api-lit.md §6.6）
  const figures = usePaperFigures(paper);
  const renderFigure = useCallback(
    (n: number) => {
      const fig = figures.find((f) => f.index === n);
      return fig ? <FigureEmbed paperId={paperId} fig={fig} /> : null;
    },
    [figures, paperId],
  );

  if (isLoading) return <div className="empty">{tr('加载论文详情…', 'Loading paper…')}</div>;
  if (isError || !paper) {
    return (
      <EmptyState
        compact
        icon="x"
        title={tr('无法加载论文详情', 'Failed to load paper')}
        desc={tr('后端不可用或该论文不存在。', 'Backend unavailable or the paper does not exist.')}
      />
    );
  }

  const arxivUrl = paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : null;
  const relevance = paper.relevance_score;
  const starred = paper.starred ?? false;
  const readingStatus: ReadingStatus = paper.reading_status ?? 'unread';

  return (
    <div className="scroll fadeup" key={paper.id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      {/* —— 元数据头 —— */}
      <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
            <PaperStatusPill status={paper.status} sm />
            {paper.venue && (
              <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
                {paper.venue}
              </span>
            )}
            {paper.has_wiki && (
              <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
                <Icon name="sparkle" size={11} />
                wiki
              </span>
            )}
            {paper.pdf_available && (
              <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                <Icon name="file" size={11} />
                PDF
              </span>
            )}
            {(paper.note_count ?? 0) > 0 && (
              <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
                <Icon name="pen" size={10} />
                {tr(`${paper.note_count} 条笔记`, `${paper.note_count} notes`)}
              </span>
            )}
          </div>
          <h1 style={{ fontSize: 20, fontWeight: 680, lineHeight: 1.3, margin: '0 0 6px', letterSpacing: '-0.01em' }}>
            {paper.title}
          </h1>
          {paper.authors.length > 0 && (
            <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.6 }}>
              {paper.authors.map((a, i) => (
                <span key={`${a.name}-${i}`}>
                  {i > 0 && <span style={{ color: 'var(--text-4)' }}> · </span>}
                  <span
                    className="author-link"
                    role="button"
                    tabIndex={0}
                    title={tr(`只看 ${a.name} 的论文`, `Show only ${a.name}'s papers`)}
                    onClick={() => onFilterAuthor(a.name)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        onFilterAuthor(a.name);
                      }
                    }}
                  >
                    {a.name}
                  </span>
                </span>
              ))}
            </div>
          )}
          {(paper.affiliations?.length ?? 0) > 0 && (
            <div className="row gap6 wrap" style={{ marginTop: 8 }}>
              <Icon name="pin" size={11} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
              {(affilsOpen ? paper.affiliations! : paper.affiliations!.slice(0, AFFIL_CHIP_LIMIT)).map((name) => (
                <span
                  key={name}
                  className="chip"
                  style={{ fontSize: 11 }}
                  title={tr(`只看 ${name} 的论文`, `Show only papers from ${name}`)}
                  {...clickable(() => onFilterAffiliation(name))}
                >
                  {name}
                </span>
              ))}
              {paper.affiliations!.length > AFFIL_CHIP_LIMIT && (
                <span className="chip" style={{ fontSize: 11 }} onClick={() => setAffilsOpen((o) => !o)}>
                  {affilsOpen
                    ? tr('收起', 'Collapse')
                    : `+${paper.affiliations!.length - AFFIL_CHIP_LIMIT}`}
                </span>
              )}
            </div>
          )}
        </div>
        {relevance !== null && (
          <ScoreRing value={relevance} max={1} size={56} label={tr('相关度', 'Relevance')} />
        )}
      </div>

      {/* —— 操作 —— */}
      <div className="row gap8 wrap" style={{ marginTop: 14 }}>
        <button className="btn btn-primary sm" onClick={() => navigate(`/papers/${paper.id}/read`, { state: readerFrom(location, 'wiki') })}>
          <Icon name="file" size={13} />
          {tr('阅读原文', 'Read original')}
        </button>
        <button
          className="btn btn-soft sm"
          title={
            paper.has_wiki
              ? tr('用最新的图文模式重写这篇介绍', 'Rewrite this intro with the latest text+figures mode')
              : tr('AI 精读并编译图文介绍', 'Have the AI read and compile an illustrated intro')
          }
          disabled={recompileMutation.isPending}
          onClick={() => recompileMutation.mutate()}
        >
          {recompileMutation.isPending ? (
            <>
              <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
              {tr('AI 编译中，约 1 分钟…', 'Compiling — about a minute…')}
            </>
          ) : (
            <>
              <Icon name="sparkle" size={13} />
              {paper.has_wiki ? tr('重新编译', 'Recompile') : tr('编译', 'Compile')}
            </>
          )}
        </button>
        {paper.has_wiki && paper.wiki_content && (
          <button
            className="btn btn-soft sm"
            title={tr('全屏阅览图文介绍，可导出 PDF', 'Full-screen reading view, exportable to PDF')}
            onClick={() => {
              setReaderPrint(false);
              setReaderOpen(true);
            }}
          >
            <Icon name="book" size={13} />
            {tr('阅览模式', 'Reading mode')}
          </button>
        )}
        <button
          className="btn btn-ghost sm"
          style={{ color: 'var(--danger-tx)' }}
          title={tr('移入垃圾桶（可召回）', 'Move to trash (restorable)')}
          disabled={deleteMutation.isPending}
          onClick={() => deleteMutation.mutate()}
        >
          <Icon name="x" size={13} />
          {tr('删除', 'Delete')}
        </button>
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
        {paper.url && !arxivUrl && (
          <a
            className="btn btn-ghost sm"
            href={paper.url}
            target="_blank"
            rel="noreferrer noopener"
            style={{ textDecoration: 'none' }}
          >
            <Icon name="link" size={13} />
            {tr('原文链接', 'Source link')}
          </a>
        )}
      </div>

      {/* —— 个人状态：星标 + 阅读状态 —— */}
      <div className="row gap12 wrap" style={{ marginTop: 12 }}>
        <button
          className="btn btn-ghost sm"
          disabled={metaMutation.isPending}
          onClick={() => metaMutation.mutate({ starred: !starred })}
          style={starred ? { color: 'var(--warn-tx)' } : undefined}
        >
          <Icon name={starred ? 'starFill' : 'star'} size={13} />
          {starred ? tr('已星标', 'Starred') : tr('加星标', 'Star')}
        </button>
        <span className="row gap8">
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
            {tr('阅读状态', 'Reading status')}
          </span>
          <Segmented<ReadingStatus>
            options={READING_STATUS.map((m) => ({ v: m.v, label: tr(m.label, m.en) }))}
            value={readingStatus}
            onChange={(v) => metaMutation.mutate({ reading_status: v })}
          />
        </span>
      </div>

      {/* —— 标签（就地编辑；库作用域，课题/独立库通用） —— */}
      <TagEditor paper={paper} scopeId={scopeId} />

      {/* —— frontmatter 风格元数据卡 —— */}
      <div className="card card-pad" style={{ margin: '18px 0 0', background: 'var(--surface-2)', padding: '14px 18px' }}>
        <MetaItem label="arxiv_id">{paper.arxiv_id ? <span className="mono">{paper.arxiv_id}</span> : <span className="muted">—</span>}</MetaItem>
        <MetaItem label="doi">{paper.doi ? <span className="mono">{paper.doi}</span> : <span className="muted">—</span>}</MetaItem>
        <MetaItem label="published">
          {paper.published_at ? <span className="mono">{paper.published_at.slice(0, 10)}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="relevance">
          {relevance !== null ? (
            <RelevanceBar value={relevance} width={140} />
          ) : (
            <span className="muted">{tr('未打分', 'not scored')}</span>
          )}
        </MetaItem>
        <MetaItem label={tr('入库时间', 'added at')}>
          <span className="mono">{fmtTime(paper.created_at)}</span>
        </MetaItem>
        <MetaItem label={tr('编译时间', 'compiled at')}>
          {paper.compiled_at ? (
            <span className="mono">{fmtTime(paper.compiled_at)}</span>
          ) : (
            <span className="muted">{tr('未编译', 'not compiled')}</span>
          )}
        </MetaItem>
      </div>

      {/* —— 概念 chips（过多时折叠） —— */}
      {paper.concepts.length > 0 && (
        <div className="row gap8 wrap" style={{ marginTop: 16 }}>
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
            <span className="chip" style={{ fontSize: 11 }} onClick={() => setConceptsOpen((o) => !o)}>
              {conceptsOpen
                ? tr('收起', 'Collapse')
                : tr(`+${paper.concepts.length - CONCEPT_CHIP_LIMIT} 个概念`, `+${paper.concepts.length - CONCEPT_CHIP_LIMIT} concepts`)}
            </span>
          )}
        </div>
      )}

      {/* —— 摘要（折叠） —— */}
      {paper.abstract && (
        <div className="card" style={{ marginTop: 18, overflow: 'hidden' }}>
          <div
            className="row"
            onClick={() => setAbstractOpen((o) => !o)}
            style={{ padding: '11px 16px', cursor: 'pointer', justifyContent: 'space-between', userSelect: 'none' }}
          >
            <span style={{ fontSize: 12.5, fontWeight: 650 }}>{tr('摘要', 'Abstract')}</span>
            <Icon
              name="chevDown"
              size={14}
              style={{ color: 'var(--text-3)', transform: abstractOpen ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
            />
          </div>
          {abstractOpen && (
            <div style={{ padding: '0 16px 14px', fontSize: 12.5, lineHeight: 1.7, color: 'var(--text-2)' }}>
              {paper.abstract}
            </div>
          )}
        </div>
      )}

      {/* —— TL;DR —— */}
      {paper.tldr && (
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
          {paper.tldr}
        </div>
      )}

      {/* —— 重要图片画廊（有图显示；正文已嵌图时默认折叠，避免重复视觉） —— */}
      <FiguresSection paper={paper} defaultCollapsed={hasEmbeddedFigures(paper.wiki_content, figures)} />

      {/* —— Wiki 正文（markdown，含 ![[fig:N]] 嵌入图） —— */}
      <div style={{ marginTop: 22 }}>
        {paper.wiki_content ? (
          <>
            <div
              className="row"
              style={{
                justifyContent: 'space-between',
                alignItems: 'center',
                paddingBottom: 10,
                marginBottom: 16,
                borderBottom: '0.5px solid var(--border)',
              }}
            >
              <div className="row gap8">
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
                  {tr('AI 图文介绍', 'AI intro')}
                </span>
                <CompileBadge model={paper.compiled_model} at={paper.compiled_at} />
              </div>
              <div className="row gap6">
                <button
                  className="btn btn-soft sm"
                  title={tr('全屏专注阅读', 'Full-screen focused reading')}
                  onClick={() => {
                    setReaderPrint(false);
                    setReaderOpen(true);
                  }}
                >
                  <Icon name="book" size={13} />
                  {tr('阅览模式', 'Reading mode')}
                </button>
                <button
                  className="btn btn-ghost sm"
                  title={tr('打开阅览页并唤起打印，另存为 PDF', 'Open the reader and print to save as PDF')}
                  onClick={() => {
                    setReaderPrint(true);
                    setReaderOpen(true);
                  }}
                >
                  <Icon name="download" size={13} />
                  {tr('导出 PDF', 'Export PDF')}
                </button>
              </div>
            </div>
            <Markdown source={paper.wiki_content} onWikiLink={onWikiLink} renderFigure={renderFigure} />
          </>
        ) : (
          <EmptyState
            compact
            icon="pen"
            title={tr('还没有 AI 介绍', 'No AI intro yet')}
            desc={tr(
              '点上方的编译按钮，让 AI 精读这篇论文并生成图文介绍。',
              'Hit the compile button above to have the AI read this paper and write an illustrated intro.',
            )}
          />
        )}
      </div>

      {readerOpen && (
        <PaperReader
          paper={paper}
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
    </div>
  );
}

/* ---------------- Tab 主体 ---------------- */

export function PapersTab({ pid, libraryId, selectedId, onSelect, onOpenConcept, onWikiLink, advSeed }: PapersTabProps) {
  const scopeId = libraryId ?? pid ?? '';
  const [view, setView] = useState<ViewFilter>('all');
  const [sort, setSort] = useState<PaperSort>('relevance');
  const [mode, setMode] = useState<SearchMode>('keyword');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());

  // —— 文献管理增强过滤器 ——
  const [tagFilter, setTagFilter] = useState('');
  const [readingFilter, setReadingFilter] = useState<'' | ReadingStatus>('');
  const [addOpen, setAddOpen] = useState(false);
  // 高级检索（作者/机构/发表时间/入库时间）
  const [advOpen, setAdvOpen] = useState(false);
  const [advAuthor, setAdvAuthor] = useState('');
  const [advAffiliation, setAdvAffiliation] = useState('');
  const [advPubFrom, setAdvPubFrom] = useState('');
  const [advPubTo, setAdvPubTo] = useState('');
  const [advCreatedFrom, setAdvCreatedFrom] = useState('');
  const [advCreatedTo, setAdvCreatedTo] = useState('');
  const author = useDebounced(advAuthor.trim());
  const affiliation = useDebounced(advAffiliation.trim());
  const advActive = !!(author || affiliation || advPubFrom || advPubTo || advCreatedFrom || advCreatedTo);

  // 点击作者/机构 → 论文库只留匹配的论文（走已有的高级检索过滤）；
  // 其余高级条件重置，面板展开让用户看到生效的条件
  const applyAdvFilter = useCallback((patch: { author?: string; affiliation?: string }) => {
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
    if (patch.author) {
      toast(tr(`已筛选作者：${patch.author}`, `Filtered by author: ${patch.author}`), 'info');
    } else if (patch.affiliation) {
      toast(tr(`已筛选机构：${patch.affiliation}`, `Filtered by affiliation: ${patch.affiliation}`), 'info');
    }
  }, [onSelect]);

  const filterByAuthor = useCallback((name: string) => applyAdvFilter({ author: name }), [applyAdvFilter]);
  const filterByAffiliation = useCallback((name: string) => applyAdvFilter({ affiliation: name }), [applyAdvFilter]);

  // 深链 /wiki?author= / ?affiliation=（阅读页信息面板跳回）：进入后自动填入高级检索并应用
  const seedSeq = advSeed?.seq ?? 0;
  useEffect(() => {
    if (!advSeed || advSeed.seq === 0) return;
    applyAdvFilter({ author: advSeed.author, affiliation: advSeed.affiliation });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedSeq]);

  // 多选（批量删除/导出）：默认关闭，底部「多选」按钮开启后行首出现复选框
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [trashOpen, setTrashOpen] = useState(false);
  const queryClient = useQueryClient();

  // 切换方向/视图/搜索时退出多选
  useEffect(() => {
    setSelected(new Set());
    setSelectMode(false);
  }, [scopeId, view, q, tagFilter, readingFilter]);

  const bulkDeleteMutation = useMutation({
    mutationFn: () => (libraryId ? api.batchDeleteLibraryPapers(libraryId, [...selected]) : api.batchDeletePapers(scopeId, [...selected])),
    onSuccess: (res) => {
      toast(tr(`已把 ${res.deleted} 篇移入垃圾桶，可召回`, `Moved ${res.deleted} papers to trash — restorable`), 'ok');
      if (selectedId && selected.has(selectedId)) onSelect('');
      setSelected(new Set());
      setSelectMode(false);
      void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
      void queryClient.invalidateQueries({ queryKey: ['project-graph', scopeId] });
    },
    onError: (e) =>
      toast(`${tr('删除失败：', 'Delete failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const bulkExportMutation = useMutation({
    mutationFn: () =>
      libraryId
        ? api.downloadLibraryCitations(libraryId, { format: 'bibtex', ids: [...selected] })
        : api.downloadCitations(pid ?? '', { format: 'bibtex', ids: [...selected] }),
    onSuccess: (blob) => {
      saveBlob(blob, 'polaris-selected.bib');
      toast(tr(`已导出 ${selected.size} 篇的 BibTeX`, `Exported BibTeX for ${selected.size} papers`), 'ok');
    },
    onError: (e) =>
      toast(`${tr('导出失败：', 'Export failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const semanticActive = mode === 'semantic' && q.length > 0;

  // —— 项目标签（过滤下拉用；接口未就绪时静默降级） ——
  const tagsQuery = useQuery({
    queryKey: ['project-tags', scopeId],
    queryFn: () => (libraryId ? api.listLibraryTags(libraryId) : api.listTags(scopeId)),
    retry: false,
  });
  const projectTags = tagsQuery.data ?? [];

  // —— 关键词/浏览：分页列表 ——
  const listQuery = useInfiniteQuery({
    queryKey: ['papers', scopeId, view, q, sort, tagFilter, readingFilter, author, affiliation, advPubFrom, advPubTo, advCreatedFrom, advCreatedTo],
    queryFn: ({ pageParam }) => {
      const opts = {
        ...viewQuery(view),
        q: q || undefined,
        sort,
        tag: tagFilter || undefined,
        reading_status: readingFilter || undefined,
        author: author || undefined,
        affiliation: affiliation || undefined,
        published_from: advPubFrom ? `${advPubFrom}T00:00:00Z` : undefined,
        published_to: advPubTo ? `${advPubTo}T23:59:59Z` : undefined,
        created_from: advCreatedFrom ? `${advCreatedFrom}T00:00:00Z` : undefined,
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

  // —— 语义检索 ——
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

  const isLoading = semanticActive ? semQuery.isLoading : listQuery.isLoading;
  const isError = semanticActive ? semQuery.isError : listQuery.isError;
  const fallbackNotice = semanticActive && semQuery.data && semQuery.data.mode_used === 'keyword';

  const hasFilter = !!q || view !== 'all' || !!tagFilter || !!readingFilter || advActive;

  // 列表变化后自动选中第一篇
  const firstId = papers[0]?.id ?? null;
  useEffect(() => {
    if (!selectedId && firstId) onSelect(firstId);
  }, [selectedId, firstId, onSelect]);

  const filterDisabled = semanticActive ? { opacity: 0.45, pointerEvents: 'none' as const } : undefined;

  return (
    <div className="split">
      {/* —— 左：列表 —— */}
      <div className="split-list">
        <div style={{ padding: '12px 14px 10px', borderBottom: '0.5px solid var(--border)' }}>
          <div className="row gap8">
            <SearchInput
              value={qInput}
              onChange={setQInput}
              placeholder={
                mode === 'semantic'
                  ? tr('语义检索（自然语言描述）…', 'Semantic search (natural language)…')
                  : tr('搜索标题 / 关键词…', 'Search title / keywords…')
              }
            />
            <Segmented<SearchMode>
              options={[
                { v: 'keyword', label: tr('关键词', 'Keyword') },
                { v: 'semantic', label: tr('语义', 'Semantic') },
              ]}
              value={mode}
              onChange={setMode}
            />
            <button
              className="icon-btn"
              style={{
                width: 28,
                height: 28,
                flexShrink: 0,
                position: 'relative',
                ...(advOpen || advActive ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : {}),
              }}
              title={tr('高级检索：作者 / 机构 / 发表时间 / 入库时间', 'Advanced search: author / affiliation / publish date / added date')}
              onClick={() => setAdvOpen((o) => !o)}
            >
              <Icon name="sliders" size={14} />
              {advActive && (
                <span
                  style={{
                    position: 'absolute',
                    top: 3,
                    right: 3,
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    background: 'var(--accent)',
                  }}
                />
              )}
            </button>
          </div>
          {advOpen && (
            <div
              className="col gap8"
              style={{
                marginTop: 8,
                padding: '10px 12px',
                borderRadius: 10,
                background: 'var(--surface-2)',
                ...filterDisabled,
              }}
            >
              <div className="row gap8">
                <input
                  className="input"
                  style={{ flex: 1, minWidth: 0, height: 28, fontSize: 11.5 }}
                  placeholder={tr('作者姓名…', 'Author name…')}
                  value={advAuthor}
                  onChange={(e) => setAdvAuthor(e.target.value)}
                />
                <input
                  className="input"
                  style={{ flex: 1, minWidth: 0, height: 28, fontSize: 11.5 }}
                  placeholder={tr('发表机构…', 'Affiliation…')}
                  title={tr(
                    '需要论文元数据带有机构信息（入库时自动从 OpenAlex 补充）',
                    'Needs affiliation metadata (auto-filled from OpenAlex on ingest)',
                  )}
                  value={advAffiliation}
                  onChange={(e) => setAdvAffiliation(e.target.value)}
                />
              </div>
              <div className="row gap6" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                <span style={{ width: 52, flexShrink: 0 }}>{tr('发表时间', 'Published')}</span>
                <input className="input" type="date" style={{ flex: 1, minWidth: 0, height: 26, fontSize: 11 }}
                  value={advPubFrom} onChange={(e) => setAdvPubFrom(e.target.value)} />
                <span>—</span>
                <input className="input" type="date" style={{ flex: 1, minWidth: 0, height: 26, fontSize: 11 }}
                  value={advPubTo} onChange={(e) => setAdvPubTo(e.target.value)} />
              </div>
              <div className="row gap6" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                <span style={{ width: 52, flexShrink: 0 }}>{tr('入库时间', 'Added')}</span>
                <input className="input" type="date" style={{ flex: 1, minWidth: 0, height: 26, fontSize: 11 }}
                  value={advCreatedFrom} onChange={(e) => setAdvCreatedFrom(e.target.value)} />
                <span>—</span>
                <input className="input" type="date" style={{ flex: 1, minWidth: 0, height: 26, fontSize: 11 }}
                  value={advCreatedTo} onChange={(e) => setAdvCreatedTo(e.target.value)} />
              </div>
              {advActive && (
                <button
                  className="btn btn-ghost sm"
                  style={{ alignSelf: 'flex-start', height: 22, fontSize: 10.5 }}
                  onClick={() => {
                    setAdvAuthor('');
                    setAdvAffiliation('');
                    setAdvPubFrom('');
                    setAdvPubTo('');
                    setAdvCreatedFrom('');
                    setAdvCreatedTo('');
                  }}
                >
                  {tr('清空高级条件', 'Clear advanced filters')}
                </button>
              )}
            </div>
          )}
          <div className="row gap6 wrap" style={{ marginTop: 10 }}>
            {VIEW_FILTERS.map((f) => (
              <span
                key={f.v}
                className={`chip${view === f.v ? ' on' : ''}`}
                style={filterDisabled}
                title={tr(f.hintZh, f.hintEn)}
                onClick={() => setView(f.v)}
              >
                {tr(f.zh, f.en)}
              </span>
            ))}
            <span
              className="chip"
              style={{ marginLeft: 'auto', gap: 5 }}
              title={tr('垃圾桶：已删除的文献，可召回或彻底删除', 'Trash: deleted papers — restore or delete forever')}
              onClick={() => setTrashOpen(true)}
            >
              <Icon name="trash" size={12} />
              {tr('垃圾桶', 'Trash')}
            </span>
          </div>
          {/* 标签（库作用域，课题/独立库通用）/ 阅读状态过滤 */}
          <div className="row gap6" style={{ marginTop: 8, ...filterDisabled }}>
            <select
              className="input"
              style={{ height: 26, fontSize: 11.5, flex: 1, minWidth: 0, padding: '0 6px' }}
              value={tagFilter}
              onChange={(e) => setTagFilter(e.target.value)}
              title={tr('按标签过滤', 'Filter by tag')}
            >
              <option value="">{tr('全部标签', 'All tags')}</option>
              {projectTags.map((t) => (
                <option key={t.id} value={t.name}>
                  {t.name}（{t.paper_count}）
                </option>
              ))}
            </select>
            <select
              className="input"
              style={{ height: 26, fontSize: 11.5, width: 88, padding: '0 6px' }}
              value={readingFilter}
              onChange={(e) => setReadingFilter(e.target.value as '' | ReadingStatus)}
              title={tr('按阅读状态过滤', 'Filter by reading status')}
            >
              <option value="">{tr('读没读', 'Read?')}</option>
              {READING_STATUS.map((m) => (
                <option key={m.v} value={m.v}>
                  {m.label}
                </option>
              ))}
            </select>
          </div>
          <div className="row gap8" style={{ marginTop: 10 }}>
            <Segmented<PaperSort>
              options={[
                { v: 'relevance', label: tr('按相关度', 'By relevance') },
                { v: '-published_at', label: tr('按时间', 'By date') },
              ]}
              value={sort}
              onChange={setSort}
            />
            <button className="btn btn-primary sm" style={{ height: 26, marginLeft: 'auto' }} onClick={() => setAddOpen(true)}>
              <Icon name="plus" size={12} />
              {tr('添加文献', 'Add paper')}
            </button>
          </div>
          {fallbackNotice && (
            <div
              style={{
                marginTop: 8,
                fontSize: 11,
                color: 'var(--warn-tx)',
                background: 'var(--warn-bg)',
                borderRadius: 7,
                padding: '5px 9px',
                lineHeight: 1.5,
              }}
            >
              {tr('语义检索暂不可用，已回退为关键词匹配。', 'Semantic search unavailable — fell back to keyword matching.')}
            </div>
          )}
        </div>

        <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
          {isLoading ? (
            <div className="empty">{tr('加载论文…', 'Loading papers…')}</div>
          ) : isError ? (
            <EmptyState
              compact
              icon="x"
              title={tr('无法加载论文列表', 'Failed to load papers')}
              desc={tr('后端不可用或接口尚未就绪，稍后重试。', 'Backend unavailable or API not ready — try again later.')}
            />
          ) : papers.length === 0 ? (
            <EmptyState
              compact
              icon="book"
              title={hasFilter ? tr('没有匹配的论文', 'No matching papers') : tr('论文库为空', 'Library is empty')}
              desc={
                hasFilter
                  ? tr('换个关键词或过滤条件试试。', 'Try a different keyword or filter.')
                  : tr(
                      '先到建库与同步页运行初始建库，或点上方的添加文献按钮手动添加。',
                      'Run the initial library build under Ingest & sync, or add papers manually with the button above.',
                    )
              }
            />
          ) : (
            <>
              {papers.map((p) => (
                <PaperRow
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
                  <button
                    className="btn btn-soft sm"
                    disabled={listQuery.isFetchingNextPage}
                    onClick={() => void listQuery.fetchNextPage()}
                  >
                    {listQuery.isFetchingNextPage ? (
                      <>
                        <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                        {tr('加载中…', 'Loading…')}
                      </>
                    ) : (
                      <>
                        <Icon name="chevDown" size={13} />
                        {tr('加载更多', 'Load more')}
                      </>
                    )}
                  </button>
                </div>
              )}
            </>
          )}
        </div>

        {/* —— 底部固定操作栏 —— */}
        <div
          className="row gap8"
          style={{ padding: '9px 14px', borderTop: '0.5px solid var(--border)', flexShrink: 0 }}
        >
          <button
            className={'btn sm ' + (selectMode ? 'btn-primary' : 'btn-ghost')}
            title={tr('开启后列表出现复选框，可批量删除 / 导出', 'Show checkboxes for bulk delete / export')}
            onClick={() => {
              setSelectMode((m) => !m);
              setSelected(new Set());
            }}
          >
            <Icon name="check" size={13} />
            {selectMode ? tr(`已选 ${selected.size}`, `${selected.size} selected`) : tr('多选', 'Select')}
          </button>
          {selectMode && (
            <>
              <button
                className="btn btn-ghost sm"
                style={{ color: 'var(--danger-tx)' }}
                disabled={selected.size === 0 || bulkDeleteMutation.isPending}
                onClick={() => bulkDeleteMutation.mutate()}
              >
                <Icon name="x" size={12} />
                {tr('删除', 'Delete')}
              </button>
              <button
                className="btn btn-ghost sm"
                disabled={selected.size === 0 || bulkExportMutation.isPending}
                onClick={() => bulkExportMutation.mutate()}
              >
                <Icon name="download" size={12} />
                {tr('导出 BibTeX', 'Export BibTeX')}
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
          />
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            {tr('从列表中选择一篇论文', 'Pick a paper from the list')}
          </div>
        )}
      </div>

      {/* —— 添加文献 Modal —— */}
      <AddPaperModal pid={pid ?? ''} libraryId={libraryId} open={addOpen} onClose={() => setAddOpen(false)} onImported={onSelect} />

      {/* —— 垃圾桶 —— */}
      <TrashModal pid={pid ?? ''} libraryId={libraryId} open={trashOpen} onClose={() => setTrashOpen(false)} />
    </div>
  );
}
