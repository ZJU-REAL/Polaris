import { useEffect, useMemo, useState } from 'react';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { ScoreRing } from '../../components/ui/ScoreRing';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import {
  api,
  ApiError,
  type PaperRead,
  type PaperSort,
  type PaperStatus,
  type SearchMode,
} from '../../lib/api';
import { categoryMeta, SearchInput, useDebounced } from './shared';

/* ============================================================
   论文库 Tab：左列表（过滤/搜索/排序/加载更多）+ 右详情
   （元数据 + wiki markdown + 概念 chips + 人工纳入/排除）。
   ============================================================ */

const PAGE_SIZE = 20;

type StatusFilter = 'all' | Extract<PaperStatus, 'candidate' | 'scored' | 'compiled' | 'excluded' | 'included'>;

const STATUS_FILTERS: { v: StatusFilter; label: string }[] = [
  { v: 'all', label: '全部' },
  { v: 'candidate', label: '候选' },
  { v: 'scored', label: '已打分' },
  { v: 'compiled', label: '已编译' },
  { v: 'included', label: '已纳入' },
  { v: 'excluded', label: '已排除' },
];

export interface PapersTabProps {
  pid: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onOpenConcept: (id: string) => void;
  /** wiki 双链 [[概念名]] 点击 → 按名称跳概念 */
  onWikiLink: WikiLinkHandler;
}

/* ---------------- 列表行 ---------------- */

function PaperRow({ p, active, onClick }: { p: PaperRead; active: boolean; onClick: () => void }) {
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

/* ---------------- 详情面板 ---------------- */

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
}: {
  paperId: string;
  pid: string;
  onOpenConcept: (id: string) => void;
  onWikiLink: WikiLinkHandler;
}) {
  const queryClient = useQueryClient();
  const [abstractOpen, setAbstractOpen] = useState(false);

  const { data: paper, isLoading, isError } = useQuery({
    queryKey: ['paper', paperId],
    queryFn: () => api.getPaper(paperId),
    retry: false,
  });

  const patchMutation = useMutation({
    mutationFn: (status: 'included' | 'excluded') => api.patchPaper(paperId, { status }),
    onSuccess: (_p, status) => {
      toast(status === 'included' ? '已纳入知识库' : '已排除该论文', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['paper', paperId] });
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', pid] });
    },
    onError: (e) => toast(`操作失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  if (isLoading) return <div className="empty">加载论文详情…</div>;
  if (isError || !paper) {
    return <EmptyState compact icon="x" title="无法加载论文详情" desc="后端不可用或该论文不存在。" />;
  }

  const authors = paper.authors.map((a) => a.name).join(' · ');
  const arxivUrl = paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : null;
  const relevance = paper.relevance_score;

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
      <div className="row gap8" style={{ marginTop: 14 }}>
        <button
          className={paper.status === 'included' ? 'btn btn-soft sm' : 'btn btn-primary sm'}
          disabled={patchMutation.isPending || paper.status === 'included'}
          onClick={() => patchMutation.mutate('included')}
        >
          <Icon name="check" size={13} />
          {paper.status === 'included' ? '已纳入' : '纳入 include'}
        </button>
        <button
          className="btn btn-ghost sm"
          disabled={patchMutation.isPending || paper.status === 'excluded'}
          onClick={() => patchMutation.mutate('excluded')}
        >
          <Icon name="x" size={13} />
          {paper.status === 'excluded' ? '已排除' : '排除 exclude'}
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
        <MetaItem label="ingested">
          <span className="mono">{fmtTime(paper.created_at)}</span>
        </MetaItem>
      </div>

      {/* —— 概念 chips —— */}
      {paper.concepts.length > 0 && (
        <div className="row gap8 wrap" style={{ marginTop: 16 }}>
          {paper.concepts.map((c) => {
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

      {/* —— Wiki 正文（markdown） —— */}
      <div style={{ marginTop: 22 }}>
        {paper.wiki_content ? (
          <Markdown source={paper.wiki_content} onWikiLink={onWikiLink} />
        ) : (
          <EmptyState
            compact
            icon="pen"
            title="尚未编译 wiki 页"
            desc="该论文还没有经过 Librarian 精读编译（相关度不足、或尚未运行初始建库 / 增量同步）。"
          />
        )}
      </div>
    </div>
  );
}

/* ---------------- Tab 主体 ---------------- */

export function PapersTab({ pid, selectedId, onSelect, onOpenConcept, onWikiLink }: PapersTabProps) {
  const [status, setStatus] = useState<StatusFilter>('all');
  const [sort, setSort] = useState<PaperSort>('relevance');
  const [mode, setMode] = useState<SearchMode>('keyword');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());

  const semanticActive = mode === 'semantic' && q.length > 0;

  // —— 关键词/浏览：分页列表 ——
  const listQuery = useInfiniteQuery({
    queryKey: ['papers', pid, status, q, sort],
    queryFn: ({ pageParam }) =>
      api.listPapers(pid, {
        status: status === 'all' ? undefined : status,
        q: q || undefined,
        sort,
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

  const total = semanticActive ? papers.length : (listQuery.data?.pages[0]?.total ?? null);
  const isLoading = semanticActive ? semQuery.isLoading : listQuery.isLoading;
  const isError = semanticActive ? semQuery.isError : listQuery.isError;
  const fallbackNotice = semanticActive && semQuery.data && semQuery.data.mode_used === 'keyword';

  // 列表变化后自动选中第一篇
  const firstId = papers[0]?.id ?? null;
  useEffect(() => {
    if (!selectedId && firstId) onSelect(firstId);
  }, [selectedId, firstId, onSelect]);

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
          </div>
          <div className="row gap6 wrap" style={{ marginTop: 10 }}>
            {STATUS_FILTERS.map((f) => (
              <span
                key={f.v}
                className={`chip${status === f.v ? ' on' : ''}`}
                style={semanticActive ? { opacity: 0.45, pointerEvents: 'none' } : undefined}
                onClick={() => setStatus(f.v)}
              >
                {f.label}
              </span>
            ))}
          </div>
          <div className="row" style={{ marginTop: 10, justifyContent: 'space-between' }}>
            <Segmented<PaperSort>
              options={[
                { v: 'relevance', label: '按相关度' },
                { v: '-published_at', label: '按时间' },
              ]}
              value={sort}
              onChange={setSort}
            />
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
              {total !== null ? `${total} 篇` : ''}
            </span>
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
              title={q ? '没有匹配的论文' : '论文库为空'}
              desc={q ? '换个关键词试试。' : '先到「建库与同步」页运行 bootstrap 回填文献。'}
            />
          ) : (
            <>
              {papers.map((p) => (
                <PaperRow key={p.id} p={p} active={p.id === selectedId} onClick={() => onSelect(p.id)} />
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
      </div>

      {/* —— 右：详情 —— */}
      <div className="split-detail">
        {selectedId ? (
          <PaperDetailPane paperId={selectedId} pid={pid} onOpenConcept={onOpenConcept} onWikiLink={onWikiLink} />
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            从左侧选择一篇论文
          </div>
        )}
      </div>
    </div>
  );
}
