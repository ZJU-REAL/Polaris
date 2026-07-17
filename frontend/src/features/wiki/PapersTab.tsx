import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { ScoreRing } from '../../components/ui/ScoreRing';
import { EmptyState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { FigureEmbed, FiguresSection, hasEmbeddedFigures, usePaperFigures } from '../../components/ui/FigureGallery';
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
import { categoryMeta, saveBlob, SearchInput, useDebounced } from './shared';
import { READING_STATUS, ReadingDot } from '../reading/shared';

/* ============================================================
   论文库 Tab：左列表（过滤/搜索/排序/加载更多 + 添加文献/导出）
   + 右详情（元数据 + wiki markdown + 概念 chips + 标签/星标/
   阅读状态 + 编译/删除 + 阅读入口）；列表支持多选批量删除/导出。
   ============================================================ */

const PAGE_SIZE = 20;

/** 论文库视图（docs/api-lit.md §8.5）：只展示相关性达标的文献（低相关论文不进库）。 */
type ViewFilter = 'all' | 'compiled' | 'starred';

const VIEW_FILTERS: { v: ViewFilter; label: string; hint?: string }[] = [
  { v: 'all', label: '全部', hint: '相关性达到阈值的全部文献' },
  { v: 'compiled', label: '已编译', hint: 'AI 已精读编译出介绍' },
  { v: 'starred', label: '已星标', hint: '我加了星标的文献' },
];

/** 视图 → 列表查询参数（低相关/未筛选论文一律不出现在论文库）。 */
function viewQuery(view: ViewFilter): { status: PaperStatusFilter; starred?: boolean } {
  if (view === 'compiled') return { status: 'compiled_any' };
  if (view === 'starred') return { status: 'library', starred: true };
  return { status: 'library' };
}

export interface PapersTabProps {
  pid: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onOpenConcept: (id: string) => void;
  /** wiki 双链 [[概念名]] 点击 → 按名称跳概念 */
  onWikiLink: WikiLinkHandler;
}

/* ---------------- 添加文献 Modal ---------------- */

type ImportMethod = 'arxiv' | 'doi' | 'bibtex';

function AddPaperModal({
  pid,
  open,
  onClose,
  onImported,
}: {
  pid: string;
  open: boolean;
  onClose: () => void;
  /** 添加成功 / 已存在时跳转选中该论文 */
  onImported: (paperId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [method, setMethod] = useState<ImportMethod>('arxiv');
  const [arxivId, setArxivId] = useState('');
  const [doi, setDoi] = useState('');
  const [bibtex, setBibtex] = useState('');
  const [parseError, setParseError] = useState<string | null>(null);

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
    mutationFn: (inp: PaperImportInput) => api.importPaper(pid, inp),
    onSuccess: (p) => {
      toast('文献已加进论文库', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', pid] });
      reset();
      onClose();
      onImported(p.id);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        const paperId = (e.body as { paper_id?: string } | null | undefined)?.paper_id;
        toast('这篇论文已经在库中，已为你打开', 'info');
        reset();
        onClose();
        if (paperId) onImported(paperId);
      } else if (e instanceof ApiError && e.status === 422) {
        setParseError(e.message.replace(/^PARSE_FAILED:?\s*/, '') || '内容解析失败，请检查格式');
      } else {
        toast(`添加失败：${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="添加文献"
      sub="手动把一篇论文加进当前研究方向的论文库"
      width={520}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>
            取消
          </button>
          <button
            className="btn btn-primary sm"
            disabled={!input || importMutation.isPending}
            onClick={() => input && importMutation.mutate(input)}
          >
            {importMutation.isPending ? (
              <>
                <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                添加中…
              </>
            ) : (
              <>
                <Icon name="plus" size={13} />
                添加
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
          { v: 'bibtex', label: 'BibTeX 粘贴' },
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
              placeholder="例如 2405.01234 或 2405.01234v2"
              value={arxivId}
              onChange={(e) => {
                setArxivId(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              填 arXiv 编号即可，标题、作者、摘要会自动抓取，并顺带下载 PDF。
            </div>
          </>
        ) : method === 'doi' ? (
          <>
            <input
              className="input mono"
              style={{ width: '100%' }}
              placeholder="例如 10.1145/3567890.1234567"
              value={doi}
              onChange={(e) => {
                setDoi(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              通过 DOI 反查论文信息（OpenAlex），适合期刊/会议论文。
            </div>
          </>
        ) : (
          <>
            <textarea
              className="textarea mono"
              style={{ width: '100%', minHeight: 150, resize: 'vertical', fontSize: 12 }}
              placeholder={'粘贴单条 BibTeX 条目，例如：\n@inproceedings{smith2024example,\n  title = {...},\n  author = {...},\n  year = {2024},\n}'}
              value={bibtex}
              onChange={(e) => {
                setBibtex(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              一次粘贴一条；title 必填，作者/年份/期刊/DOI 能解析多少取多少。
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
            解析失败：{parseError}
          </div>
        )}
      </div>
    </Modal>
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
      toast('Obsidian 笔记库已导出', 'ok');
    },
    onError: (e) => toast(`导出失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const citationsMutation = useMutation({
    mutationFn: (format: CitationFormat) => api.downloadCitations(pid, { format, ...filters }),
    onSuccess: (blob, format) => {
      saveBlob(blob, format === 'bibtex' ? 'polaris-references.bib' : 'polaris-references.json');
      toast(format === 'bibtex' ? 'BibTeX 文件已导出' : 'CSL-JSON 文件已导出', 'ok');
    },
    onError: (e) => toast(`导出失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
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
        导出
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
              Obsidian 笔记库
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
              BibTeX 引用
              <span className="muted" style={{ marginLeft: 5, fontSize: 10.5 }}>.bib · 全部库内文献</span>
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
              <span className="muted" style={{ marginLeft: 5, fontSize: 10.5 }}>Zotero 可直接导入</span>
            </span>
          </button>
        </div>
      )}
    </div>
  );
}

/* ---------------- 垃圾桶 ---------------- */

function TrashModal({ pid, open, onClose }: { pid: string; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const [trashQ, setTrashQ] = useState('');

  const trashQuery = useQuery({
    queryKey: ['papers-trash', pid],
    queryFn: () => api.listPapers(pid, { status: 'excluded', size: 100, sort: '-published_at' }),
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
    void queryClient.invalidateQueries({ queryKey: ['papers-trash', pid] });
    void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
    void queryClient.invalidateQueries({ queryKey: ['ingest-state', pid] });
    void queryClient.invalidateQueries({ queryKey: ['project-graph', pid] });
  };

  const restoreMutation = useMutation({
    mutationFn: (id: string) => api.restorePaper(id),
    onSuccess: (p) => {
      toast(`已召回：${p.title.slice(0, 30)}`, 'ok');
      invalidate();
    },
    onError: (e) => toast(`召回失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const purgeMutation = useMutation({
    mutationFn: (id: string) => api.deletePaper(id),
    onSuccess: () => {
      toast('已彻底删除', 'ok');
      invalidate();
    },
    onError: (e) => toast(`删除失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const emptyMutation = useMutation({
    mutationFn: () => api.emptyTrash(pid),
    onSuccess: (res) => {
      toast(`垃圾桶已清空（${res.deleted} 篇）`, 'ok');
      setConfirmEmpty(false);
      invalidate();
    },
    onError: (e) => toast(`清空失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const busy = restoreMutation.isPending || purgeMutation.isPending || emptyMutation.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="垃圾桶"
      sub="已删除的文献（含相关性不足自动删除的）"
      width={640}
      footer={
        <>
          {confirmEmpty ? (
            <>
              <span style={{ fontSize: 12, color: 'var(--danger-tx)', marginRight: 'auto' }}>
                将彻底删除全部 {allItems.length} 篇及其文件，无法恢复
              </span>
              <button className="btn btn-ghost sm" onClick={() => setConfirmEmpty(false)}>
                取消
              </button>
              <button
                className="btn btn-primary sm"
                style={{ background: 'var(--danger-tx)' }}
                disabled={busy}
                onClick={() => emptyMutation.mutate()}
              >
                {emptyMutation.isPending ? '清空中…' : '确认清空'}
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
                清空垃圾桶
              </button>
              <button className="btn btn-soft sm" onClick={onClose}>
                关闭
              </button>
            </>
          )}
        </>
      }
    >
      {trashQuery.isLoading ? (
        <div className="empty" style={{ padding: 24 }}>加载中…</div>
      ) : allItems.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>垃圾桶是空的</div>
      ) : (
        <div className="col" style={{ gap: 6 }}>
          <SearchInput value={trashQ} onChange={setTrashQ} placeholder="搜索标题 / 作者…" />
          {items.length === 0 && <div className="empty" style={{ padding: 16 }}>没有匹配的文献</div>}
          {items.map((p) => (
            <div
              key={p.id}
              className="row gap8"
              style={{
                padding: '8px 10px',
                borderRadius: 9,
                background: 'var(--surface-2)',
                alignItems: 'flex-start',
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12.5, fontWeight: 600, lineHeight: 1.45, overflowWrap: 'break-word' }}>
                  {p.title}
                </div>
                {p.authors.length > 0 && (
                  <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2, lineHeight: 1.5 }}>
                    {p.authors.map((a) => a.name).join(' · ')}
                  </div>
                )}
                <div className="row gap8" style={{ marginTop: 3 }}>
                  {p.year !== null && <span className="mono muted" style={{ fontSize: 10.5 }}>{p.year}</span>}
                  {p.relevance_score !== null && (
                    <span className="mono muted" style={{ fontSize: 10.5 }}>
                      相关度 {(p.relevance_score * 10).toFixed(1)}
                    </span>
                  )}
                  {p.has_wiki && (
                    <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent-text)' }}>有介绍</span>
                  )}
                </div>
              </div>
              <button
                className="btn btn-soft sm"
                style={{ height: 26, flexShrink: 0 }}
                disabled={busy}
                title="召回到论文库"
                onClick={() => restoreMutation.mutate(p.id)}
              >
                <Icon name="refresh" size={12} />
                召回
              </button>
              <button
                className="btn btn-ghost sm"
                style={{ height: 26, flexShrink: 0, color: 'var(--danger-tx)' }}
                disabled={busy}
                title="彻底删除（连同文件，无法恢复）"
                onClick={() => purgeMutation.mutate(p.id)}
              >
                <Icon name="x" size={12} />
                彻底删除
              </button>
            </div>
          ))}
          {(trashQuery.data?.total ?? 0) > items.length && (
            <div className="muted" style={{ fontSize: 11, textAlign: 'center', padding: 6 }}>
              仅显示最近 {items.length} 篇（共 {trashQuery.data?.total} 篇）
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

/* ---------------- 列表行 ---------------- */

function PaperRow({
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
        {selectMode && (
          <input
            type="checkbox"
            checked={checked}
            onClick={(e) => e.stopPropagation()}
            onChange={onToggleCheck}
            title="选中后可批量删除 / 导出"
            style={{ width: 13, height: 13, margin: 0, flexShrink: 0, accentColor: 'var(--accent)', cursor: 'pointer' }}
          />
        )}
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
      <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: 'var(--text)' }}>{p.title}</div>
      <div className="row gap8" style={{ marginTop: 6 }}>
        <StatusPill status={p.status} sm />
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
          <span className="row" style={{ gap: 3, fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }} title={`${p.note_count} 条笔记`}>
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
}

/* ---------------- 标签就地编辑 ---------------- */

function TagEditor({ paper, pid }: { paper: PaperDetail; pid: string }) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [value, setValue] = useState('');
  const tags = paper.tags ?? [];

  const putMutation = useMutation({
    mutationFn: (names: string[]) => api.putPaperTags(paper.id, names),
    onSuccess: (p) => {
      queryClient.setQueryData<PaperDetail>(['paper', paper.id], p);
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
      void queryClient.invalidateQueries({ queryKey: ['project-tags', pid] });
    },
    onError: (e) => toast(`标签更新失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
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
        标签
      </span>
      {tags.map((t) => (
        <span key={t} className="tag" style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          {t}
          <span
            title="移除标签"
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
          placeholder="标签名，回车确定"
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
          <Icon name="plus" size={10} style={{ display: 'inline-block', verticalAlign: -1 }} /> 标签
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

function PaperDetailPane({
  paperId,
  pid,
  onOpenConcept,
  onWikiLink,
  onDeleted,
}: {
  paperId: string;
  pid: string;
  onOpenConcept: (id: string) => void;
  onWikiLink: WikiLinkHandler;
  /** 删除成功后回调（父组件清空选中，自动跳到列表第一篇） */
  onDeleted: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [abstractOpen, setAbstractOpen] = useState(false);
  const [conceptsOpen, setConceptsOpen] = useState(false);

  const { data: paper, isLoading, isError } = useQuery({
    queryKey: ['paper', paperId],
    queryFn: () => api.getPaper(paperId),
    retry: false,
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.patchPaper(paperId, { status: 'excluded' }),
    onSuccess: () => {
      toast('已移入垃圾桶，可在列表底部的垃圾桶中召回', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
      void queryClient.invalidateQueries({ queryKey: ['papers-trash', pid] });
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', pid] });
      void queryClient.invalidateQueries({ queryKey: ['project-graph', pid] });
      onDeleted();
    },
    onError: (e) => toast(`删除失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // 星标 / 阅读状态（个人视角）
  const metaMutation = useMutation({
    mutationFn: (input: Partial<MyMeta>) => api.putMyMeta(paperId, input),
    onSuccess: (meta) => {
      queryClient.setQueryData<PaperDetail>(['paper', paperId], (old) =>
        old ? { ...old, starred: meta.starred, reading_status: meta.reading_status } : old,
      );
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
    },
    onError: (e) => toast(`更新失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // 重新编译：用最新的图文模式重写 wiki 页（同步调用，约 1 分钟）
  const recompileMutation = useMutation({
    mutationFn: () => api.recompilePaper(paperId),
    onSuccess: () => {
      toast('编译完成，介绍已更新', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['paper', paperId] });
      void queryClient.invalidateQueries({ queryKey: ['paper-figures', paperId] });
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
    },
    onError: (e) => toast(`重新编译失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
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

  if (isLoading) return <div className="empty">加载论文详情…</div>;
  if (isError || !paper) {
    return <EmptyState compact icon="x" title="无法加载论文详情" desc="后端不可用或该论文不存在。" />;
  }

  const authors = paper.authors.map((a) => a.name).join(' · ');
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
            <StatusPill status={paper.status} sm />
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
                {paper.note_count} 条笔记
              </span>
            )}
          </div>
          <h1 style={{ fontSize: 20, fontWeight: 680, lineHeight: 1.3, margin: '0 0 6px', letterSpacing: '-0.01em' }}>
            {paper.title}
          </h1>
          {authors && <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.5 }}>{authors}</div>}
        </div>
        {relevance !== null && (
          <ScoreRing value={Math.round(relevance * 100) / 10} size={56} label="相关度" />
        )}
      </div>

      {/* —— 操作 —— */}
      <div className="row gap8 wrap" style={{ marginTop: 14 }}>
        <button className="btn btn-primary sm" onClick={() => navigate(`/papers/${paper.id}/read`)}>
          <Icon name="book" size={13} />
          阅读
        </button>
        <button
          className="btn btn-soft sm"
          title={paper.has_wiki ? '用最新的图文模式重写这篇介绍' : 'AI 精读并编译图文介绍'}
          disabled={recompileMutation.isPending}
          onClick={() => recompileMutation.mutate()}
        >
          {recompileMutation.isPending ? (
            <>
              <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
              AI 编译中，约 1 分钟…
            </>
          ) : (
            <>
              <Icon name="sparkle" size={13} />
              {paper.has_wiki ? '重新编译' : '编译'}
            </>
          )}
        </button>
        <button
          className="btn btn-ghost sm"
          style={{ color: 'var(--danger-tx)' }}
          title="移入垃圾桶（可召回）"
          disabled={deleteMutation.isPending}
          onClick={() => deleteMutation.mutate()}
        >
          <Icon name="x" size={13} />
          删除
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
            原文链接
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
          {starred ? '已星标' : '加星标'}
        </button>
        <span className="row gap8">
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
            阅读状态
          </span>
          <Segmented<ReadingStatus>
            options={READING_STATUS.map((m) => ({ v: m.v, label: m.label }))}
            value={readingStatus}
            onChange={(v) => metaMutation.mutate({ reading_status: v })}
          />
        </span>
      </div>

      {/* —— 标签（就地编辑） —— */}
      <TagEditor paper={paper} pid={pid} />

      {/* —— frontmatter 风格元数据卡 —— */}
      <div className="card card-pad" style={{ margin: '18px 0 0', background: 'var(--surface-2)', padding: '14px 18px' }}>
        <MetaItem label="arxiv_id">{paper.arxiv_id ? <span className="mono">{paper.arxiv_id}</span> : <span className="muted">—</span>}</MetaItem>
        <MetaItem label="doi">{paper.doi ? <span className="mono">{paper.doi}</span> : <span className="muted">—</span>}</MetaItem>
        <MetaItem label="published">
          {paper.published_at ? <span className="mono">{paper.published_at.slice(0, 10)}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="relevance">
          {relevance !== null ? <RelevanceBar value={relevance} width={140} /> : <span className="muted">未打分</span>}
        </MetaItem>
        <MetaItem label="入库时间">
          <span className="mono">{fmtTime(paper.created_at)}</span>
        </MetaItem>
        <MetaItem label="编译时间">
          {paper.compiled_at ? (
            <span className="mono">{fmtTime(paper.compiled_at)}</span>
          ) : (
            <span className="muted">未编译</span>
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
                <span style={{ opacity: 0.6, marginLeft: 5, fontSize: '0.85em' }}>{meta.zh}</span>
              </span>
            );
          })}
          {paper.concepts.length > CONCEPT_CHIP_LIMIT && (
            <span className="chip" style={{ fontSize: 11 }} onClick={() => setConceptsOpen((o) => !o)}>
              {conceptsOpen ? '收起' : `+${paper.concepts.length - CONCEPT_CHIP_LIMIT} 个概念`}
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
            <span style={{ fontSize: 12.5, fontWeight: 650 }}>
              摘要 <span className="en-label" style={{ fontSize: 11 }}>Abstract</span>
            </span>
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
          <Markdown source={paper.wiki_content} onWikiLink={onWikiLink} renderFigure={renderFigure} />
        ) : (
          <EmptyState
            compact
            icon="pen"
            title="还没有 AI 介绍"
            desc="点上方的编译按钮，让 AI 精读这篇论文并生成图文介绍。"
          />
        )}
      </div>

    </div>
  );
}

/* ---------------- Tab 主体 ---------------- */

export function PapersTab({ pid, selectedId, onSelect, onOpenConcept, onWikiLink }: PapersTabProps) {
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

  // 多选（批量删除/导出）：默认关闭，底部「多选」按钮开启后行首出现复选框
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [trashOpen, setTrashOpen] = useState(false);
  const queryClient = useQueryClient();

  // 切换方向/视图/搜索时退出多选
  useEffect(() => {
    setSelected(new Set());
    setSelectMode(false);
  }, [pid, view, q, tagFilter, readingFilter]);

  const bulkDeleteMutation = useMutation({
    mutationFn: () => api.batchDeletePapers(pid, [...selected]),
    onSuccess: (res) => {
      toast(`已把 ${res.deleted} 篇移入垃圾桶，可召回`, 'ok');
      if (selectedId && selected.has(selectedId)) onSelect('');
      setSelected(new Set());
      setSelectMode(false);
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', pid] });
      void queryClient.invalidateQueries({ queryKey: ['project-graph', pid] });
    },
    onError: (e) => toast(`删除失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const bulkExportMutation = useMutation({
    mutationFn: () => api.downloadCitations(pid, { format: 'bibtex', ids: [...selected] }),
    onSuccess: (blob) => {
      saveBlob(blob, 'polaris-selected.bib');
      toast(`已导出 ${selected.size} 篇的 BibTeX`, 'ok');
    },
    onError: (e) => toast(`导出失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const semanticActive = mode === 'semantic' && q.length > 0;

  // —— 项目标签（过滤下拉用；接口未就绪时静默降级） ——
  const tagsQuery = useQuery({
    queryKey: ['project-tags', pid],
    queryFn: () => api.listTags(pid),
    retry: false,
  });
  const projectTags = tagsQuery.data ?? [];

  // —— 关键词/浏览：分页列表 ——
  const listQuery = useInfiniteQuery({
    queryKey: ['papers', pid, view, q, sort, tagFilter, readingFilter, author, affiliation, advPubFrom, advPubTo, advCreatedFrom, advCreatedTo],
    queryFn: ({ pageParam }) =>
      api.listPapers(pid, {
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
      }),
    initialPageParam: 1,
    getNextPageParam: (last) => (last.page * last.size < last.total ? last.page + 1 : undefined),
    retry: false,
    enabled: !semanticActive,
  });

  // —— 语义检索 ——
  const semQuery = useQuery({
    queryKey: ['wiki-search', pid, q],
    queryFn: () => api.searchProject(pid, { q, mode: 'semantic', limit: 30 }),
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
              placeholder={mode === 'semantic' ? '语义检索（自然语言描述）…' : '搜索标题 / 关键词…'}
            />
            <Segmented<SearchMode>
              options={[
                { v: 'keyword', label: '关键词' },
                { v: 'semantic', label: '语义' },
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
              title="高级检索：作者 / 机构 / 发表时间 / 入库时间"
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
                  placeholder="作者姓名…"
                  value={advAuthor}
                  onChange={(e) => setAdvAuthor(e.target.value)}
                />
                <input
                  className="input"
                  style={{ flex: 1, minWidth: 0, height: 28, fontSize: 11.5 }}
                  placeholder="发表机构…"
                  title="需要论文元数据带有机构信息（入库时自动从 OpenAlex 补充）"
                  value={advAffiliation}
                  onChange={(e) => setAdvAffiliation(e.target.value)}
                />
              </div>
              <div className="row gap6" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                <span style={{ width: 52, flexShrink: 0 }}>发表时间</span>
                <input className="input" type="date" style={{ flex: 1, minWidth: 0, height: 26, fontSize: 11 }}
                  value={advPubFrom} onChange={(e) => setAdvPubFrom(e.target.value)} />
                <span>—</span>
                <input className="input" type="date" style={{ flex: 1, minWidth: 0, height: 26, fontSize: 11 }}
                  value={advPubTo} onChange={(e) => setAdvPubTo(e.target.value)} />
              </div>
              <div className="row gap6" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                <span style={{ width: 52, flexShrink: 0 }}>入库时间</span>
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
                  清空高级条件
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
                title={f.hint}
                onClick={() => setView(f.v)}
              >
                {f.label}
              </span>
            ))}
            <span
              className="chip"
              style={{ marginLeft: 'auto' }}
              title="垃圾桶：已删除的文献，可召回或彻底删除"
              onClick={() => setTrashOpen(true)}
            >
              垃圾桶
            </span>
          </div>
          {/* 标签 / 阅读状态 / 仅星标 过滤 */}
          <div className="row gap6" style={{ marginTop: 8, ...filterDisabled }}>
            <select
              className="input"
              style={{ height: 26, fontSize: 11.5, flex: 1, minWidth: 0, padding: '0 6px' }}
              value={tagFilter}
              onChange={(e) => setTagFilter(e.target.value)}
              title="按标签过滤"
            >
              <option value="">全部标签</option>
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
              title="按阅读状态过滤"
            >
              <option value="">读没读</option>
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
                { v: 'relevance', label: '按相关度' },
                { v: '-published_at', label: '按时间' },
              ]}
              value={sort}
              onChange={setSort}
            />
            <button className="btn btn-primary sm" style={{ height: 26, marginLeft: 'auto' }} onClick={() => setAddOpen(true)}>
              <Icon name="plus" size={12} />
              添加文献
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
              语义检索暂不可用，已回退为关键词匹配。
            </div>
          )}
        </div>

        <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
          {isLoading ? (
            <div className="empty">加载论文…</div>
          ) : isError ? (
            <EmptyState compact icon="x" title="无法加载论文列表" desc="后端不可用或接口尚未就绪，稍后重试。" />
          ) : papers.length === 0 ? (
            <EmptyState
              compact
              icon="book"
              title={hasFilter ? '没有匹配的论文' : '论文库为空'}
              desc={hasFilter ? '换个关键词或过滤条件试试。' : '先到建库与同步页运行初始建库，或点上方的添加文献按钮手动添加。'}
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
                        加载中…
                      </>
                    ) : (
                      <>
                        <Icon name="chevDown" size={13} />
                        加载更多
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
            title="开启后列表出现复选框，可批量删除 / 导出"
            onClick={() => {
              setSelectMode((m) => !m);
              setSelected(new Set());
            }}
          >
            <Icon name="check" size={13} />
            {selectMode ? `已选 ${selected.size}` : '多选'}
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
                删除
              </button>
              <button
                className="btn btn-ghost sm"
                disabled={selected.size === 0 || bulkExportMutation.isPending}
                onClick={() => bulkExportMutation.mutate()}
              >
                <Icon name="download" size={12} />
                导出 BibTeX
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
            pid={pid}
            onOpenConcept={onOpenConcept}
            onWikiLink={onWikiLink}
            onDeleted={() => onSelect('')}
          />
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            从左侧选择一篇论文
          </div>
        )}
      </div>

      {/* —— 添加文献 Modal —— */}
      <AddPaperModal pid={pid} open={addOpen} onClose={() => setAddOpen(false)} onImported={onSelect} />

      {/* —— 垃圾桶 —— */}
      <TrashModal pid={pid} open={trashOpen} onClose={() => setTrashOpen(false)} />
    </div>
  );
}
