import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { api, type ShelfImportInput, type ShelfItemRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { topicPath, useProject } from '../../app/project';
import { SearchInput, useDebounced } from '../wiki/shared';
import { libraryPath, useTopicLibrary } from '../libraries/hooks';

/* ============================================================
   /t/:topicId/research — 课题「相关研究」书架：
   从方向文献库挑论文入架（引用为主、入架落 wiki 快照兜底），
   或用 arXiv 编号 / DOI 个人补充入库；每篇可写「为什么相关」备注。
   入架同时自动收藏进「我的文献库」；移出书架不动个人库。
   ============================================================ */

const PAGE_SIZE = 50;

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 快照日期 → 「7 月 22 日」 / "Jul 22"。 */
function fmtSnapshotDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return tr(
    `${d.getMonth() + 1} 月 ${d.getDate()} 日`,
    d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
  );
}

/** 手动添加输入解析：DOI（10.xxx / doi.org 链接）之外一律按 arXiv 编号处理。 */
function parseImportInput(raw: string): ShelfImportInput | null {
  let v = raw.trim();
  if (!v) return null;
  if (v.includes('doi.org/')) v = v.slice(v.indexOf('doi.org/') + 'doi.org/'.length);
  if (/^10\.\d{4,}/.test(v)) return { doi: v };
  if (v.includes('arxiv.org/')) {
    const seg = v.split('/').filter(Boolean);
    v = (seg[seg.length - 1] ?? '').replace(/\.pdf$/, '');
  }
  return v ? { arxiv_id: v } : null;
}

/* ---------------- wiki 来源徽标 ---------------- */

function WikiBadge({ item }: { item: ShelfItemRead }) {
  if (item.wiki_source === 'live') {
    return (
      <span
        className="mono"
        style={{
          fontSize: 10,
          color: 'var(--ok-tx)',
          background: 'var(--ok-bg)',
          padding: '1px 7px',
          borderRadius: 999,
          flexShrink: 0,
        }}
      >
        {tr('解读可用', 'Wiki available')}
      </span>
    );
  }
  if (item.wiki_source === 'personal') {
    return (
      <span
        className="mono"
        title={tr(
          '这篇论文没有公共库版解读，下面显示的是你自己生成的个人版。',
          'No shared library wiki for this paper; showing the personal version you generated.',
        )}
        style={{
          fontSize: 10,
          color: 'var(--accent-text)',
          background: 'var(--accent-soft)',
          padding: '1px 7px',
          borderRadius: 999,
          flexShrink: 0,
        }}
      >
        {tr('个人版解读', 'Personal wiki')}
      </span>
    );
  }
  if (item.wiki_source === 'snapshot') {
    return (
      <span
        className="mono"
        title={tr(
          '这篇论文的库版解读已不可用（被移除或重编译），下面显示的是入架时的快照。',
          'The library wiki is no longer available; showing the snapshot taken when it was shelved.',
        )}
        style={{
          fontSize: 10,
          color: 'var(--warn-tx)',
          background: 'var(--warn-bg)',
          padding: '1px 7px',
          borderRadius: 999,
          flexShrink: 0,
        }}
      >
        {tr(
          `库中版本不可用 · ${fmtSnapshotDate(item.snapshot_at)}快照`,
          `Library copy unavailable · snapshot ${fmtSnapshotDate(item.snapshot_at)}`,
        )}
      </span>
    );
  }
  return null;
}

/* ---------------- 书架卡片（备注内联编辑） ---------------- */

function ShelfCard({
  item,
  busy,
  generating,
  refreshing,
  onOpen,
  onSaveNote,
  onRemove,
  onGenerateWiki,
  onRefreshSnapshot,
}: {
  item: ShelfItemRead;
  busy: boolean;
  /** 本卡的个人版 wiki 正在生成 */
  generating: boolean;
  /** 本卡的快照正在刷新 */
  refreshing: boolean;
  onOpen: () => void;
  onSaveNote: (note: string | null) => void;
  onRemove: () => void;
  onGenerateWiki: () => void;
  onRefreshSnapshot: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.note ?? '');
  const authors = item.authors.map((a) => a.name).join(', ');

  const save = () => {
    const next = draft.trim() || null;
    setEditing(false);
    if (next !== (item.note ?? null)) onSaveNote(next);
  };

  return (
    <div className="card" style={{ padding: '14px 16px' }}>
      <div className="row gap8" style={{ marginBottom: 5 }}>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
          {item.arxiv_id ?? item.venue ?? '—'}
        </span>
        {item.year !== null && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{item.year}</span>
        )}
        <WikiBadge item={item} />
        {item.wiki_source === 'snapshot' && (
          <button
            className="icon-btn"
            title={tr(
              '刷新快照：重新拷一份当前可得的最新解读（库版优先，其次个人版）',
              'Refresh snapshot: re-copy the latest available wiki (library first, then personal)',
            )}
            disabled={refreshing}
            onClick={onRefreshSnapshot}
            style={{ width: 22, height: 22 }}
          >
            <Icon name="refresh" size={12} />
          </button>
        )}
        {item.wiki_source === 'none' && (
          <button
            className="btn btn-soft sm"
            title={tr('用 AI 生成这篇论文的个人版解读（将使用你的模型额度）', 'Generate a personal wiki with AI (uses your model quota)')}
            disabled={generating}
            onClick={onGenerateWiki}
            style={{ height: 22, fontSize: 10.5, padding: '0 8px' }}
          >
            <Icon name="sparkle" size={11} />
            {generating ? tr('生成中…', 'Generating…') : tr('生成 wiki', 'Generate wiki')}
          </button>
        )}
        <span style={{ marginLeft: 'auto' }} />
        <button
          className="icon-btn"
          title={tr('移出相关研究（个人库收藏保留）', 'Remove from related work (kept in my library)')}
          disabled={busy}
          onClick={onRemove}
        >
          <Icon name="trash" size={14} />
        </button>
      </div>

      <div
        role="button"
        tabIndex={0}
        onClick={onOpen}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onOpen();
          }
        }}
        title={tr('打开阅读页', 'Open the reading page')}
        style={{ fontSize: 13.5, fontWeight: 620, lineHeight: 1.35, cursor: 'pointer', color: 'var(--text)' }}
      >
        {item.title}
      </div>
      {(authors || item.venue) && (
        <div
          title={authors}
          style={{
            fontSize: 11.5,
            color: 'var(--text-3)',
            marginTop: 3,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {authors}
          {authors && item.venue && item.arxiv_id ? ` · ${item.venue}` : ''}
        </div>
      )}

      {/* —— 备注：点击进入编辑，失焦/⌘Enter 保存 —— */}
      {editing ? (
        <textarea
          className="input"
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save();
            if (e.key === 'Escape') {
              setDraft(item.note ?? '');
              setEditing(false);
            }
          }}
          placeholder={tr('这篇为什么和课题相关？', 'Why is this paper relevant?')}
          style={{ marginTop: 8, width: '100%', minHeight: 54, fontSize: 12, lineHeight: 1.5, resize: 'vertical' }}
        />
      ) : (
        <button
          onClick={() => {
            setDraft(item.note ?? '');
            setEditing(true);
          }}
          title={tr('编辑备注', 'Edit note')}
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: 6,
            width: '100%',
            marginTop: 8,
            padding: '6px 8px',
            border: 'none',
            borderRadius: 8,
            background: item.note ? 'var(--surface-2)' : 'transparent',
            cursor: 'text',
            textAlign: 'left',
            fontSize: 12,
            lineHeight: 1.5,
            fontFamily: 'var(--sans)',
            color: item.note ? 'var(--text-2)' : 'var(--text-4)',
          }}
        >
          <Icon name="pen" size={12} style={{ marginTop: 2, flexShrink: 0, color: 'var(--text-4)' }} />
          <span style={{ whiteSpace: 'pre-wrap' }}>
            {item.note ?? tr('添加备注：这篇为什么相关…', 'Add a note: why is this relevant…')}
          </span>
        </button>
      )}
    </div>
  );
}

/* ---------------- 页面 ---------------- */

export function ResearchPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { currentProjectId } = useProject();
  const pid = currentProjectId ?? '';
  // 文献库入口：课题隐式库详情页（列表未就绪时退回旧 /wiki 路径由重定向兜底）
  const topicLib = useTopicLibrary(pid || null);
  const wikiHref = topicLib ? libraryPath(topicLib.id) : topicPath(pid, 'wiki');

  const [page, setPage] = useState(1);
  const [searchOpen, setSearchOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  const [importInput, setImportInput] = useState('');
  const importRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => setPage(1), [pid]);

  const shelfQuery = useQuery({
    queryKey: ['shelf', pid, page],
    queryFn: () => api.listShelf(pid, { page, size: PAGE_SIZE }),
    enabled: !!pid,
    retry: false,
    placeholderData: keepPreviousData,
  });
  const idsQuery = useQuery({
    queryKey: ['shelf-ids', pid],
    queryFn: () => api.listShelfIds(pid),
    enabled: !!pid,
    retry: false,
  });
  const shelvedIds = new Set(idsQuery.data?.paper_ids ?? []);

  const searchQuery = useQuery({
    queryKey: ['shelf-search', pid, q],
    queryFn: () => api.searchProject(pid, { q, limit: 8 }),
    enabled: !!pid && searchOpen && q.length > 0,
    retry: false,
    placeholderData: keepPreviousData,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['shelf', pid] });
    void queryClient.invalidateQueries({ queryKey: ['shelf-ids', pid] });
    // 入架同步收藏进个人库
    void queryClient.invalidateQueries({ queryKey: ['library'] });
    void queryClient.invalidateQueries({ queryKey: ['library-state'] });
  };

  const addMutation = useMutation({
    mutationFn: (paperId: string) => api.addToShelf(pid, { paper_id: paperId }),
    onSuccess: () => {
      toast(tr('已加入相关研究', 'Added to related work'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('入架失败：', 'Failed to add: ')}${errText(e)}`, 'error'),
  });

  const importMutation = useMutation({
    mutationFn: (input: ShelfImportInput) => api.importToShelf(pid, input),
    onSuccess: () => {
      toast(tr('已添加到相关研究', 'Added to related work'), 'ok');
      setImportInput('');
      invalidate();
    },
    onError: (e) => toast(`${tr('添加失败：', 'Failed to add: ')}${errText(e)}`, 'error'),
  });

  const noteMutation = useMutation({
    mutationFn: ({ paperId, note }: { paperId: string; note: string | null }) =>
      api.updateShelfNote(pid, paperId, note),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['shelf', pid] }),
    onError: (e) => toast(`${tr('备注保存失败：', 'Failed to save note: ')}${errText(e)}`, 'error'),
  });

  const removeMutation = useMutation({
    mutationFn: (paperId: string) => api.removeFromShelf(pid, paperId),
    onSuccess: () => {
      toast(tr('已移出相关研究（个人库收藏保留）', 'Removed (still saved in my library)'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('移除失败：', 'Failed to remove: ')}${errText(e)}`, 'error'),
  });

  // 个人版 wiki 按需生成（wiki_source=none 的论文；费用记个人额度）
  const generateMutation = useMutation({
    mutationFn: (paperId: string) => api.compilePersonalWiki(paperId, pid),
    onSuccess: () => {
      toast(tr('个人版解读已生成', 'Personal wiki generated'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('生成失败：', 'Failed to generate: ')}${errText(e)}`, 'error'),
  });

  const refreshSnapshotMutation = useMutation({
    mutationFn: (paperId: string) => api.refreshShelfSnapshot(pid, paperId),
    onSuccess: () => {
      toast(tr('快照已刷新', 'Snapshot refreshed'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['shelf', pid] });
    },
    onError: (e) => toast(`${tr('刷新失败：', 'Failed to refresh: ')}${errText(e)}`, 'error'),
  });

  const submitImport = () => {
    const input = parseImportInput(importInput);
    if (!input) {
      toast(tr('先输入 arXiv 编号或 DOI', 'Enter an arXiv ID or DOI first'), 'info');
      return;
    }
    importMutation.mutate(input);
  };

  const data = shelfQuery.data;
  const items = data?.items ?? [];
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;
  const busy = addMutation.isPending || removeMutation.isPending || noteMutation.isPending;

  return (
    <div style={{ padding: '26px 30px', maxWidth: 860 }}>
      <PageHead
        eyebrow={tr('课题研究', 'Topic')}
        title={tr('相关研究', 'Related Work')}
        sub={tr(
          '这个课题直接依赖的论文书架：从文献库挑选，或手动补充库外论文。',
          'Papers this topic builds on: pick from the library, or add ones outside it.',
        )}
        right={
          <>
            <button
              className={'btn sm ' + (searchOpen ? 'btn-primary' : 'btn-soft')}
              onClick={() => {
                setSearchOpen((o) => !o);
                setImportOpen(false);
              }}
            >
              <Icon name="search" size={13} />
              {tr('从文献库添加', 'Add from library')}
            </button>
            <button
              className={'btn sm ' + (importOpen ? 'btn-primary' : 'btn-soft')}
              onClick={() => {
                setImportOpen((o) => !o);
                setSearchOpen(false);
              }}
            >
              <Icon name="plus" size={13} />
              {tr('手动添加', 'Add manually')}
            </button>
          </>
        }
      />

      {/* —— 从文献库添加：按当前课题库检索，一键入架 —— */}
      {searchOpen && (
        <div className="card" style={{ padding: 14, marginBottom: 16 }}>
          <div className="row gap10">
            <SearchInput
              value={qInput}
              onChange={setQInput}
              placeholder={tr('搜文献库：标题 / 摘要 / 解读…', 'Search the library: title / abstract / wiki…')}
            />
            <button className="btn btn-ghost sm" onClick={() => navigate(wikiHref)}>
              {tr('去文献库', 'Open the library')}
            </button>
          </div>
          {q.length > 0 && (
            <div className="col" style={{ marginTop: 10 }}>
              {searchQuery.isLoading ? (
                <div className="empty" style={{ padding: 14 }}>{tr('搜索中…', 'Searching…')}</div>
              ) : (searchQuery.data?.papers ?? []).length === 0 ? (
                <div className="empty" style={{ padding: 14 }}>
                  {tr('文献库里没搜到，试试「手动添加」', 'Nothing found in the library — try “Add manually”')}
                </div>
              ) : (
                (searchQuery.data?.papers ?? []).map((p) => {
                  const added = shelvedIds.has(p.id);
                  return (
                    <div
                      key={p.id}
                      className="row gap10"
                      style={{ padding: '8px 4px', borderBottom: '0.5px solid var(--border)' }}
                    >
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 12.5, fontWeight: 600, lineHeight: 1.35 }}>{p.title}</div>
                        <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 2 }}>
                          {p.arxiv_id ?? p.venue ?? '—'}
                          {p.year !== null ? ` · ${p.year}` : ''}
                        </div>
                      </div>
                      {added ? (
                        <span className="row gap6" style={{ fontSize: 11.5, color: 'var(--ok-tx)', flexShrink: 0 }}>
                          <Icon name="check" size={13} />
                          {tr('已入架', 'Added')}
                        </span>
                      ) : (
                        <button
                          className="btn btn-soft sm"
                          disabled={addMutation.isPending}
                          onClick={() => addMutation.mutate(p.id)}
                        >
                          <Icon name="plus" size={12} />
                          {tr('入架', 'Add')}
                        </button>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>
      )}

      {/* —— 手动添加：arXiv 编号 / DOI —— */}
      {importOpen && (
        <div className="card" style={{ padding: 14, marginBottom: 16 }}>
          <div className="row gap10">
            <input
              ref={importRef}
              className="input"
              autoFocus
              style={{ height: 32, fontSize: 12.5, flex: 1, minWidth: 0 }}
              value={importInput}
              onChange={(e) => setImportInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submitImport();
              }}
              placeholder={tr('arXiv 编号（如 2401.12345）或 DOI（如 10.1234/abc）', 'arXiv ID (e.g. 2401.12345) or DOI (e.g. 10.1234/abc)')}
            />
            <button className="btn btn-primary sm" disabled={importMutation.isPending} onClick={submitImport}>
              {importMutation.isPending ? tr('解析中…', 'Resolving…') : tr('添加', 'Add')}
            </button>
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 8, lineHeight: 1.5 }}>
            {tr(
              '文献库里没有的论文也能加：平台会自动抓取元数据和 PDF，只进这个课题的相关研究和你的个人文献库，不影响公共文献库。',
              'Papers outside the library work too: metadata and PDF are fetched automatically. They go to this topic and your personal library only — the shared library is untouched.',
            )}
          </div>
        </div>
      )}

      {/* —— 书架列表 —— */}
      {shelfQuery.isLoading ? (
        <div className="col gap12">
          <div className="skel" style={{ height: 110 }} />
          <div className="skel" style={{ height: 110 }} />
        </div>
      ) : shelfQuery.isError ? (
        <EmptyState
          icon="x"
          title={tr('加载不出书架', 'Cannot load the shelf')}
          desc={tr('后端暂时不可用，稍后再试。', 'The backend is unavailable — try again later.')}
          action={
            <button className="btn btn-soft sm" onClick={() => void shelfQuery.refetch()}>
              {tr('重试', 'Retry')}
            </button>
          }
        />
      ) : items.length === 0 ? (
        <EmptyState
          icon="pin"
          title={tr('书架还空着', 'The shelf is empty')}
          desc={tr(
            '去文献库逛逛，把相关论文加进来；或直接输入 arXiv 编号添加。',
            'Browse the library and shelve relevant papers, or add one by arXiv ID.',
          )}
          action={
            <div className="row gap10">
              <button className="btn btn-primary sm" onClick={() => navigate(wikiHref)}>
                <Icon name="book" size={13} />
                {tr('去文献库', 'Open the library')}
              </button>
              <button
                className="btn btn-soft sm"
                onClick={() => {
                  setImportOpen(true);
                  setSearchOpen(false);
                  setTimeout(() => importRef.current?.focus(), 0);
                }}
              >
                <Icon name="plus" size={13} />
                {tr('手动添加', 'Add manually')}
              </button>
            </div>
          }
        />
      ) : (
        <div className="col gap12">
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
            {tr(`共 ${data?.total ?? items.length} 篇`, `${data?.total ?? items.length} papers`)}
          </div>
          {items.map((item) => (
            <ShelfCard
              key={item.paper_id}
              item={item}
              busy={busy}
              generating={generateMutation.isPending && generateMutation.variables === item.paper_id}
              refreshing={
                refreshSnapshotMutation.isPending && refreshSnapshotMutation.variables === item.paper_id
              }
              onOpen={() => navigate(`/papers/${item.paper_id}/read`)}
              onSaveNote={(note) => noteMutation.mutate({ paperId: item.paper_id, note })}
              onRemove={() => removeMutation.mutate(item.paper_id)}
              onGenerateWiki={() => generateMutation.mutate(item.paper_id)}
              onRefreshSnapshot={() => refreshSnapshotMutation.mutate(item.paper_id)}
            />
          ))}
          {totalPages > 1 && (
            <div className="row gap10" style={{ justifyContent: 'center', padding: '6px 0' }}>
              <button className="btn btn-ghost sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                {tr('上一页', 'Prev')}
              </button>
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)' }}>
                {page} / {totalPages}
              </span>
              <button
                className="btn btn-ghost sm"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
              >
                {tr('下一页', 'Next')}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
