import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { api, type ConceptCategory, type ConceptRead } from '../../lib/api';
import { categoryMeta, CONCEPT_CATEGORY, SearchInput, Section, useDebounced } from './shared';

/* ============================================================
   概念库 Tab：左列表（category 过滤 + 搜索）+ 右详情
   （定义 + wiki markdown + 引用论文 + 相关概念）。
   ============================================================ */

type CategoryFilter = 'all' | ConceptCategory;

const CATEGORY_FILTERS: CategoryFilter[] = [
  'all',
  'method',
  'architecture',
  'methodology',
  'problem',
  'metric',
  'dataset',
  'other',
];

export interface ConceptsTabProps {
  pid: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onOpenPaper: (id: string) => void;
  /** wiki 双链 [[概念名]] 点击 → 按名称跳概念 */
  onWikiLink: WikiLinkHandler;
}

function ConceptRow({ c, active, onClick }: { c: ConceptRead; active: boolean; onClick: () => void }) {
  const meta = categoryMeta(c.category);
  return (
    <div
      onClick={onClick}
      style={{
        padding: '11px 16px',
        cursor: 'pointer',
        borderBottom: '0.5px solid var(--border)',
        background: active ? 'var(--accent-soft)' : 'transparent',
        borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
        transition: 'background 0.12s',
      }}
    >
      <div className="row gap8" style={{ marginBottom: 4 }}>
        <span className="pill sm" style={{ background: meta.bg, color: meta.c }}>
          {meta.zh}
        </span>
        <span className="mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-3)' }}>
          {c.paper_count} 篇
        </span>
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.3 }}>{c.name}</div>
      {c.definition && (
        <div
          style={{
            fontSize: 11.5,
            color: 'var(--text-3)',
            marginTop: 3,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {c.definition}
        </div>
      )}
    </div>
  );
}

function ConceptDetailPane({
  conceptId,
  onOpenPaper,
  onOpenConcept,
  onWikiLink,
}: {
  conceptId: string;
  onOpenPaper: (id: string) => void;
  onOpenConcept: (id: string) => void;
  onWikiLink: WikiLinkHandler;
}) {
  const { data: concept, isLoading, isError } = useQuery({
    queryKey: ['concept', conceptId],
    queryFn: () => api.getConcept(conceptId),
    retry: false,
  });

  if (isLoading) return <div className="empty">加载概念详情…</div>;
  if (isError || !concept) {
    return <EmptyState compact icon="x" title="无法加载概念详情" desc="后端不可用或该概念不存在。" />;
  }

  const meta = categoryMeta(concept.category);

  return (
    <div className="scroll fadeup" key={concept.id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      <div className="row gap8" style={{ marginBottom: 10 }}>
        <span className="pill" style={{ background: meta.bg, color: meta.c }}>
          <span className="dot" />
          {meta.zh} concept
        </span>
        <span className="mono muted" style={{ fontSize: 11 }}>
          {concept.paper_count} 篇论文引用
        </span>
      </div>
      <h1 style={{ fontSize: 22, fontWeight: 680, lineHeight: 1.25, margin: '2px 0 0', letterSpacing: '-0.01em' }}>
        {concept.name}
      </h1>

      {concept.definition && (
        <div className="card card-pad" style={{ margin: '18px 0 0', background: 'var(--surface-2)' }}>
          <div className="row gap8" style={{ marginBottom: 8 }}>
            <Icon name="sparkle" size={14} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: 12, fontWeight: 700 }}>
              定义 <span className="en-label" style={{ fontSize: 11 }}>Definition</span>
            </span>
          </div>
          <p style={{ fontSize: 13.5, lineHeight: 1.65, margin: 0, color: 'var(--text)' }}>{concept.definition}</p>
        </div>
      )}

      {concept.wiki_content && (
        <div style={{ marginTop: 20 }}>
          <Markdown source={concept.wiki_content} onWikiLink={onWikiLink} />
        </div>
      )}

      <Section title={<>出现于论文 <span className="en-label" style={{ fontSize: 11 }}>grounded in</span></>}>
        {concept.papers.length ? (
          <div className="col gap8">
            {concept.papers.map((p) => (
              <div
                key={p.id}
                className="card hoverable"
                onClick={() => onOpenPaper(p.id)}
                style={{ padding: '10px 14px' }}
              >
                <div className="row gap8">
                  <span style={{ fontSize: 12.5, fontWeight: 600, lineHeight: 1.4, flex: 1, minWidth: 0 }}>{p.title}</span>
                  {p.year !== null && (
                    <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
                      {p.year}
                    </span>
                  )}
                  <Icon name="arrow" size={13} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
                </div>
              </div>
            ))}
          </div>
        ) : (
          <span className="muted" style={{ fontSize: 12.5 }}>
            暂无引用论文
          </span>
        )}
      </Section>

      {concept.related.length > 0 && (
        <Section title={<>相关概念 <span className="en-label" style={{ fontSize: 11 }}>related</span></>}>
          <div className="row gap8 wrap">
            {concept.related.map((r) => (
              <span key={r.id} className="wikilink" style={{ height: 24 }} onClick={() => onOpenConcept(r.id)}>
                {r.name}
              </span>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

export function ConceptsTab({ pid, selectedId, onSelect, onOpenPaper, onWikiLink }: ConceptsTabProps) {
  const queryClient = useQueryClient();
  const [category, setCategory] = useState<CategoryFilter>('all');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());

  const { data, isLoading, isError } = useQuery({
    queryKey: ['concepts', pid, category, q],
    queryFn: () =>
      api.listConcepts(pid, {
        category: category === 'all' ? undefined : category,
        q: q || undefined,
      }),
    retry: false,
  });
  const concepts = useMemo(() => data ?? [], [data]);

  // 全库概念补建：编译过但概念没上链的历史论文（比如批量任务中断）从这里补
  const relinkMutation = useMutation({
    mutationFn: () => api.relinkConcepts(pid),
    onSuccess: (r) => {
      if (r.concepts_created === 0 && r.links_created === 0) {
        toast(`已检查 ${r.papers} 篇论文，概念关联都是全的`, 'info');
      } else {
        toast(`新建 ${r.concepts_created} 个概念，补上 ${r.links_created} 条论文关联`, 'ok');
      }
      void queryClient.invalidateQueries({ queryKey: ['concepts', pid] });
      void queryClient.invalidateQueries({ queryKey: ['concept'] });
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
      void queryClient.invalidateQueries({ queryKey: ['paper'] });
    },
    onError: (e) => toast(`提取失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const firstId = concepts[0]?.id ?? null;
  useEffect(() => {
    if (!selectedId && firstId) onSelect(firstId);
  }, [selectedId, firstId, onSelect]);

  return (
    <div className="split">
      <div className="split-list">
        <div style={{ padding: '12px 14px 10px', borderBottom: '0.5px solid var(--border)' }}>
          <div className="row gap8">
            <SearchInput value={qInput} onChange={setQInput} placeholder="搜索概念…" />
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
              {concepts.length ? `${concepts.length} 个` : ''}
            </span>
            <button
              className="icon-btn"
              style={{ width: 26, height: 26, borderRadius: 7, flexShrink: 0 }}
              title="从已编译论文提取概念：重抽正文 [[双链]]，补建缺失的概念和关联"
              disabled={relinkMutation.isPending}
              onClick={() => relinkMutation.mutate()}
            >
              <Icon
                name="refresh"
                size={13}
                style={relinkMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined}
              />
            </button>
          </div>
          <div className="row gap6 wrap" style={{ marginTop: 10 }}>
            {CATEGORY_FILTERS.map((f) => {
              const label = f === 'all' ? '全部' : CONCEPT_CATEGORY[f].zh;
              return (
                <span key={f} className={`chip${category === f ? ' on' : ''}`} onClick={() => setCategory(f)}>
                  {label}
                </span>
              );
            })}
          </div>
        </div>

        <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
          {isLoading ? (
            <div className="empty">加载概念…</div>
          ) : isError ? (
            <EmptyState compact icon="x" title="无法加载概念列表" desc="后端不可用或接口尚未就绪，稍后重试。" />
          ) : concepts.length === 0 ? (
            <EmptyState
              compact
              icon="layers"
              title={q ? '没有匹配的概念' : '概念库为空'}
              desc={q ? '换个关键词试试。' : '编译论文解读时会自动提取概念；已有解读的论文可以点下面按钮补一次。'}
              action={
                q ? undefined : (
                  <button
                    className="btn btn-soft sm"
                    disabled={relinkMutation.isPending}
                    onClick={() => relinkMutation.mutate()}
                  >
                    <Icon name="refresh" size={13} />
                    {relinkMutation.isPending ? '提取中…' : '从已编译论文提取概念'}
                  </button>
                )
              }
            />
          ) : (
            concepts.map((c) => (
              <ConceptRow key={c.id} c={c} active={c.id === selectedId} onClick={() => onSelect(c.id)} />
            ))
          )}
        </div>
      </div>

      <div className="split-detail">
        {selectedId ? (
          <ConceptDetailPane
            conceptId={selectedId}
            onOpenPaper={onOpenPaper}
            onOpenConcept={onSelect}
            onWikiLink={onWikiLink}
          />
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            从左侧选择一个概念
          </div>
        )}
      </div>
    </div>
  );
}
