import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { api, type ConceptCategory, type ConceptRead } from '../../lib/api';
import { clickable } from '../../lib/a11y';
import { tr } from '../../lib/i18n';
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
          {tr(meta.zh, meta.en)}
        </span>
        <span className="mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-3)' }}>
          {c.paper_count} {tr('篇', 'papers')}
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

  if (isLoading) return <div className="empty">{tr('加载概念详情…', 'Loading concept…')}</div>;
  if (isError || !concept) {
    return (
      <EmptyState
        compact
        icon="x"
        title={tr('无法加载概念详情', 'Failed to load concept')}
        desc={tr('后端不可用或该概念不存在。', 'Backend unavailable or the concept does not exist.')}
      />
    );
  }

  const meta = categoryMeta(concept.category);

  return (
    <div className="scroll fadeup" key={concept.id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      <div className="row gap8" style={{ marginBottom: 10 }}>
        <span className="pill" style={{ background: meta.bg, color: meta.c }}>
          <span className="dot" />
          {tr(`${meta.zh}概念`, `${meta.en} concept`)}
        </span>
        <span className="mono muted" style={{ fontSize: 11 }}>
          {tr(`${concept.paper_count} 篇论文引用`, `cited by ${concept.paper_count} papers`)}
        </span>
      </div>
      <h1 style={{ fontSize: 22, fontWeight: 680, lineHeight: 1.25, margin: '2px 0 0', letterSpacing: '-0.01em' }}>
        {concept.name}
      </h1>

      {concept.definition && (
        <div className="card card-pad" style={{ margin: '18px 0 0', background: 'var(--surface-2)' }}>
          <div className="row gap8" style={{ marginBottom: 8 }}>
            <Icon name="sparkle" size={14} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: 12, fontWeight: 700 }}>{tr('定义', 'Definition')}</span>
          </div>
          <p style={{ fontSize: 13.5, lineHeight: 1.65, margin: 0, color: 'var(--text)' }}>{concept.definition}</p>
        </div>
      )}

      {concept.wiki_content && (
        <div style={{ marginTop: 20 }}>
          <Markdown source={concept.wiki_content} onWikiLink={onWikiLink} />
        </div>
      )}

      <Section title={tr('出现于论文', 'Grounded in papers')}>
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
            {tr('暂无引用论文', 'No citing papers yet')}
          </span>
        )}
      </Section>

      {concept.related.length > 0 && (
        <Section title={tr('相关概念', 'Related concepts')}>
          <div className="row gap8 wrap">
            {concept.related.map((r) => (
              <span key={r.id} className="wikilink" style={{ height: 24 }} {...clickable(() => onOpenConcept(r.id))}>
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
        toast(
          tr(`已检查 ${r.papers} 篇论文，概念关联都是全的`, `Checked ${r.papers} papers — concept links all complete`),
          'info',
        );
      } else {
        toast(
          tr(
            `新建 ${r.concepts_created} 个概念，补上 ${r.links_created} 条论文关联`,
            `Created ${r.concepts_created} concepts, added ${r.links_created} paper links`,
          ),
          'ok',
        );
      }
      void queryClient.invalidateQueries({ queryKey: ['concepts', pid] });
      void queryClient.invalidateQueries({ queryKey: ['concept'] });
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
      void queryClient.invalidateQueries({ queryKey: ['paper'] });
    },
    onError: (e) =>
      toast(`${tr('提取失败：', 'Extraction failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
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
            <SearchInput value={qInput} onChange={setQInput} placeholder={tr('搜索概念…', 'Search concepts…')} />
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
              {concepts.length ? tr(`${concepts.length} 个`, `${concepts.length}`) : ''}
            </span>
            <button
              className="icon-btn"
              style={{ width: 26, height: 26, borderRadius: 7, flexShrink: 0 }}
              title={tr(
                '从已编译论文提取概念：重抽正文 [[双链]]，补建缺失的概念和关联',
                'Extract concepts from compiled papers: re-scan [[wiki links]] and fill in missing concepts and links',
              )}
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
              const label = f === 'all' ? tr('全部', 'All') : tr(CONCEPT_CATEGORY[f].zh, CONCEPT_CATEGORY[f].en);
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
            <div className="empty">{tr('加载概念…', 'Loading concepts…')}</div>
          ) : isError ? (
            <EmptyState
              compact
              icon="x"
              title={tr('无法加载概念列表', 'Failed to load concepts')}
              desc={tr('后端不可用或接口尚未就绪，稍后重试。', 'Backend unavailable or API not ready — try again later.')}
            />
          ) : concepts.length === 0 ? (
            <EmptyState
              compact
              icon="layers"
              title={q ? tr('没有匹配的概念', 'No matching concepts') : tr('概念库为空', 'No concepts yet')}
              desc={
                q
                  ? tr('换个关键词试试。', 'Try a different keyword.')
                  : tr(
                      '编译论文解读时会自动提取概念；已有解读的论文可以点下面按钮补一次。',
                      'Concepts are extracted automatically when papers are compiled; use the button below to backfill compiled papers.',
                    )
              }
              action={
                q ? undefined : (
                  <button
                    className="btn btn-soft sm"
                    disabled={relinkMutation.isPending}
                    onClick={() => relinkMutation.mutate()}
                  >
                    <Icon name="refresh" size={13} />
                    {relinkMutation.isPending
                      ? tr('提取中…', 'Extracting…')
                      : tr('从已编译论文提取概念', 'Extract concepts from compiled papers')}
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
            {tr('从左侧选择一个概念', 'Pick a concept from the list')}
          </div>
        )}
      </div>
    </div>
  );
}
