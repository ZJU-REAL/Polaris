import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import {
  api,
  type LiteratureOaCache,
  type LiteratureSearchHit,
  type LiteratureSearchRun,
  type LiteratureSearchRunDetail,
  type LiteratureTranslation,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import {
  dispatchablePolarisExtensionPapers,
  dispatchPolarisExtensionBatch,
  type PolarisExtensionPaper,
} from '../../lib/polaris-extension';

const TERMINAL = new Set(['completed', 'partial', 'failed', 'cancelled']);
const CURRENT_YEAR = new Date().getFullYear();
const PAGE_SIZE = 20;

type HitSort = 'relevance' | 'novelty' | 'impact' | 'recent' | 'title';

const STATUS_LABELS: Record<string, [string, string]> = {
  queued: ['等待执行', 'Queued'],
  running: ['检索中', 'Running'],
  completed: ['已完成', 'Completed'],
  partial: ['部分完成', 'Partial'],
  failed: ['失败', 'Failed'],
  cancelled: ['已取消', 'Cancelled'],
};

const SOURCE_LABELS: Record<string, string> = {
  arxiv: 'arXiv',
  pubmed: 'PubMed',
  europepmc: 'Europe PMC',
  openalex: 'OpenAlex',
  semantic: 'Semantic Scholar',
  crossref: 'Crossref',
  sciverse: 'Sciverse',
  unpaywall: 'Unpaywall',
  core: 'CORE',
  hal: 'HAL',
  base: 'BASE',
};

const SCORE_DIMENSIONS: Array<[string, string, string]> = [
  ['relevance', '主题相关性', 'Relevance'],
  ['evidence_quality', '证据质量', 'Evidence quality'],
  ['impact', '学术影响', 'Impact'],
  ['novelty', '创新潜力', 'Novelty'],
  ['recency', '时效性', 'Recency'],
];

function numeric(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function percentage(value: unknown): number | null {
  const number = numeric(value);
  if (number === null) return null;
  return Math.round(Math.max(0, Math.min(1, number)) * 100);
}

function sourceName(value: string): string {
  return SOURCE_LABELS[value.toLowerCase()] ?? value;
}

function authorNames(hit: LiteratureSearchHit): string {
  const names = (hit.authors ?? []).flatMap((author) => {
    const name = author.name ?? author.display_name ?? author.author_name;
    return typeof name === 'string' && name.trim() ? [name.trim()] : [];
  });
  if (!names.length) return tr('作者未知', 'Unknown authors');
  return names.length > 4 ? `${names.slice(0, 4).join(', ')} et al.` : names.join(', ');
}

function scoreReasons(hit: LiteratureSearchHit): string[] {
  const reasons = hit.scores?.reasons;
  if (Array.isArray(reasons)) return reasons.map(String).filter(Boolean);
  const reason = hit.scores?.rationale ?? hit.scores?.reason;
  return typeof reason === 'string' && reason.trim() ? [reason.trim()] : [];
}

function venueLabels(hit: LiteratureSearchHit): string[] {
  const metrics = hit.venue_metric_snapshot;
  if (!metrics) return [];
  const values: string[] = [];
  const quartile = metrics.jcr_quartile ?? metrics.quartile;
  const casZone = metrics.cas_upgraded_zone ?? metrics.cas_zone;
  const impactFactor = metrics.impact_factor;
  if (typeof quartile === 'string' && quartile.trim()) values.push(`JCR ${quartile.toUpperCase()}`);
  if ((typeof casZone === 'string' || typeof casZone === 'number') && String(casZone).trim()) {
    values.push(`中科院 ${String(casZone).includes('区') ? casZone : `${casZone}区`}`);
  }
  if (metrics.cas_top === true) values.push('Top');
  if (typeof impactFactor === 'number') values.push(`IF ${impactFactor.toFixed(2)}`);
  return [...new Set(values)].slice(0, 4);
}

function translatedFields(translation: LiteratureTranslation | undefined) {
  return translation?.status === 'ready' ? translation.translated_fields : null;
}

function progressPercent(run: LiteratureSearchRunDetail): number {
  const progress = run.progress ?? {};
  const explicit = numeric(progress.percent);
  if (explicit !== null) return Math.round(Math.max(0, Math.min(100, explicit)));
  if (run.status === 'completed' || run.status === 'partial') return 100;
  if (run.status === 'failed' || run.status === 'cancelled') return 100;
  const phase = String(progress.phase ?? run.status);
  if (phase === 'queued') return 4;
  if (phase === 'retrieving') {
    const done = numeric(progress.query_completed) ?? 0;
    const total = numeric(progress.query_total) ?? 0;
    return total > 0 ? Math.round(8 + (done / total) * 57) : 12;
  }
  if (phase === 'ranking') return numeric(progress.pending_rerank) === 0 ? 88 : 74;
  return run.status === 'running' ? 10 : 0;
}

function progressMessage(run: LiteratureSearchRunDetail): string {
  const progress = run.progress ?? {};
  const phase = String(progress.phase ?? run.status);
  if (phase === 'queued') return tr('任务已保存，等待检索 Worker 接收', 'Saved and waiting for a search worker');
  if (phase === 'retrieving') {
    const source = typeof progress.source === 'string' ? sourceName(progress.source) : null;
    const done = numeric(progress.query_completed);
    const total = numeric(progress.query_total);
    const suffix = done !== null && total !== null ? ` · ${done}/${total}` : '';
    return source
      ? tr(`正在从 ${source} 检索${suffix}`, `Searching ${source}${suffix}`)
      : tr('正在执行多源检索', 'Searching multiple sources');
  }
  if (phase === 'ranking') {
    const count = numeric(progress.pending_rerank) ?? numeric(progress.deduplicated);
    return count !== null
      ? tr(`正在复核与精排 ${count} 篇候选文献`, `Reranking ${count} candidates`)
      : tr('正在复核候选文献并生成分层结果', 'Reranking and tiering candidates');
  }
  if (run.status === 'completed') return tr('检索完成，结果已持久化', 'Search completed and results saved');
  if (run.status === 'partial') return tr('部分来源失败，其余结果已保存', 'Some sources failed; available results were saved');
  if (run.status === 'failed') return tr('检索失败，请查看来源状态', 'Search failed; inspect source status');
  if (run.status === 'cancelled') return tr('检索已取消', 'Search cancelled');
  return tr('正在准备检索', 'Preparing search');
}

export function eligibleExtensionHits(
  hits: LiteratureSearchHit[],
  selected: ReadonlySet<string>,
  oaCache: ReadonlyMap<string, LiteratureOaCache>,
): LiteratureSearchHit[] {
  return hits.filter((hit) => (
    selected.has(hit.id)
    && hit.status === 'promoted'
    && !!hit.paper_id
    && oaCache.get(hit.id)?.status !== 'ready'
  ));
}

export function LiteratureDiscoveryPanel({
  libraryId,
  readOnly = false,
}: {
  libraryId: string;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const [topic, setTopic] = useState('');
  const [requestedCount, setRequestedCount] = useState(50);
  const [startYear, setStartYear] = useState(CURRENT_YEAR - 10);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [source, setSource] = useState('');
  const [hitStatus, setHitStatus] = useState<'' | LiteratureSearchHit['status']>('');
  const [sort, setSort] = useState<HitSort>('relevance');
  const [yearFrom, setYearFrom] = useState<number | ''>('');
  const [yearTo, setYearTo] = useState<number | ''>('');
  const [page, setPage] = useState(1);
  const [translationIds, setTranslationIds] = useState<Set<string>>(new Set());

  const libraryQuery = useQuery({
    queryKey: ['library', libraryId],
    queryFn: () => api.getLibrary(libraryId),
    enabled: !!libraryId,
  });
  const runsQuery = useQuery({
    queryKey: ['literature-runs', libraryId],
    queryFn: () => api.listLiteratureRuns(libraryId, { size: 100 }),
    enabled: !!libraryId,
    refetchInterval: 5_000,
  });
  const runs = runsQuery.data?.items ?? [];
  const selectedRunId = activeRunId ?? runs[0]?.id ?? null;

  useEffect(() => {
    setPage(1);
    setSelected(new Set());
  }, [selectedRunId, query, source, hitStatus, sort, yearFrom, yearTo]);

  const runQuery = useQuery({
    queryKey: ['literature-run', libraryId, selectedRunId],
    queryFn: () => api.getLiteratureRun(libraryId, selectedRunId!),
    enabled: !!selectedRunId,
    refetchInterval: (state) => {
      const status = state.state.data?.status;
      return status && !TERMINAL.has(status) ? 2_000 : false;
    },
  });
  const hitsQuery = useQuery({
    queryKey: ['literature-hits', libraryId, selectedRunId, query, source, hitStatus, sort, yearFrom, yearTo, page],
    queryFn: () => api.listLiteratureHits(libraryId, selectedRunId!, {
      q: query.trim() || undefined,
      source: source || undefined,
      status: hitStatus || undefined,
      sort,
      year_from: yearFrom === '' ? undefined : yearFrom,
      year_to: yearTo === '' ? undefined : yearTo,
      page,
      size: PAGE_SIZE,
    }),
    enabled: !!selectedRunId,
    refetchInterval: runQuery.data && !TERMINAL.has(runQuery.data.status) ? 2_000 : false,
  });
  const oaQuery = useQuery({
    queryKey: ['literature-oa-cache', libraryId, selectedRunId],
    queryFn: () => api.listLiteratureOaCache(libraryId, selectedRunId!),
    enabled: !!selectedRunId,
    refetchInterval: runQuery.data && !TERMINAL.has(runQuery.data.status) ? 3_000 : false,
  });
  const oaByHit = useMemo(
    () => new Map((oaQuery.data ?? []).map((item) => [item.hit_id, item])),
    [oaQuery.data],
  );

  const translationQueries = useQueries({
    queries: [...translationIds].map((hitId) => ({
      queryKey: ['literature-translation', libraryId, selectedRunId, hitId],
      queryFn: () => api.getLiteratureTranslation(libraryId, selectedRunId!, hitId),
      enabled: !!selectedRunId,
      retry: false,
      refetchInterval: (state: { state: { data?: LiteratureTranslation } }) => {
        const status = state.state.data?.status;
        return status === 'queued' || status === 'running' ? 1_500 : false;
      },
    })),
  });
  const translations = useMemo(() => {
    const values = new Map<string, LiteratureTranslation>();
    translationQueries.forEach((result) => {
      if (result.data) values.set(result.data.hit_id, result.data);
    });
    return values;
  }, [translationQueries]);

  const startMutation = useMutation({
    mutationFn: async () => {
      const fallback = libraryQuery.data?.statement?.trim() || libraryQuery.data?.name || '';
      const created = await api.createLiteratureRun(libraryId, {
        topic: topic.trim() || fallback,
        requested_count: requestedCount,
        start_year: startYear,
        end_year: CURRENT_YEAR,
      });
      await api.startLiteratureRun(libraryId, created.id);
      return created;
    },
    onSuccess: (run) => {
      setActiveRunId(run.id);
      void queryClient.invalidateQueries({ queryKey: ['literature-runs', libraryId] });
      toast(tr('检索任务已进入队列', 'Search queued'), 'ok');
    },
    onError: (error) => toast(error instanceof Error ? error.message : tr('无法开始检索', 'Could not start search'), 'error'),
  });
  const translateMutation = useMutation({
    mutationFn: (hitIds: string[]) => api.translateLiteratureHits(libraryId, selectedRunId!, hitIds),
    onSuccess: (rows) => {
      setTranslationIds((old) => new Set([...old, ...rows.map((row) => row.hit_id)]));
      rows.forEach((row) => queryClient.setQueryData(
        ['literature-translation', libraryId, selectedRunId, row.hit_id],
        row,
      ));
      toast(tr('翻译任务已提交', 'Translation queued'), 'ok');
    },
    onError: (error) => toast(error instanceof Error ? error.message : tr('翻译失败', 'Translation failed'), 'error'),
  });
  const promoteMutation = useMutation({
    mutationFn: (hitIds: string[]) => api.promoteLiteratureHits(libraryId, selectedRunId!, hitIds),
    onSuccess: (items) => {
      setSelected(new Set());
      void queryClient.invalidateQueries({ queryKey: ['literature-hits', libraryId, selectedRunId] });
      void queryClient.invalidateQueries({ queryKey: ['library-papers', libraryId] });
      toast(tr(`已将 ${items.length} 篇文献加入论文库`, `${items.length} papers added to the library`), 'ok');
    },
    onError: () => toast(tr('筛选入库失败', 'Could not add selected papers'), 'error'),
  });
  const cacheMutation = useMutation({
    mutationFn: (hitIds: string[]) => api.cacheLiteratureOaPdfs(libraryId, selectedRunId!, hitIds),
    onSuccess: (items) => {
      void queryClient.invalidateQueries({ queryKey: ['literature-oa-cache', libraryId, selectedRunId] });
      const ready = items.filter((item) => item.status === 'ready').length;
      toast(tr(`开放 PDF 缓存完成：${ready}/${items.length}`, `OA cache complete: ${ready}/${items.length}`), 'ok');
    },
    onError: () => toast(tr('开放 PDF 缓存失败', 'OA PDF caching failed'), 'error'),
  });
  const extensionMutation = useMutation({
    mutationFn: async (items: LiteratureSearchHit[]) => {
      const papers: PolarisExtensionPaper[] = items.map((hit) => ({
        libraryId,
        paperId: hit.paper_id!,
        title: hit.title,
        doi: hit.doi,
        articleUrl: hit.url,
        pdfCandidates: hit.pdf_url ? [{ url: hit.pdf_url, source: hit.source, kind: 'oa' }] : [],
      }));
      const batch = await api.createDownloadBatch(items.map((hit) => ({
        library_id: libraryId,
        paper_id: hit.paper_id!,
        article_url: hit.url,
        pdf_candidates: hit.pdf_url ? [{ url: hit.pdf_url, source: hit.source, kind: 'oa' }] : [],
      })));
      const dispatchable = dispatchablePolarisExtensionPapers(papers, batch.items);
      const acknowledged = dispatchable.length > 0
        ? await dispatchPolarisExtensionBatch({ batchId: batch.id, papers: dispatchable })
        : false;
      return { batch, acknowledged, dispatchedCount: dispatchable.length };
    },
    onSuccess: ({ batch, acknowledged, dispatchedCount }) => {
      setSelected(new Set());
      const skipped = batch.items.filter((item) => item.status === 'skipped').length;
      toast(
        dispatchedCount === 0
          ? tr('所选论文均已有可读 PDF，未向扩展发送重复下载任务', 'Every selected paper already has a readable PDF; no duplicate extension task was sent')
          : acknowledged
          ? tr(`已推送 1 个扩展任务，共 ${batch.item_count} 篇${skipped ? `，后端跳过 ${skipped} 篇已有 PDF` : ''}`, `Sent one extension batch with ${batch.item_count} papers`)
          : tr('下载批次已保存；扩展未即时确认，可稍后通过 API Key 认领', 'Batch saved; the extension can claim it later with the API key'),
        dispatchedCount === 0 || acknowledged ? 'ok' : 'info',
      );
    },
    onError: () => toast(tr('无法创建扩展下载批次', 'Could not create extension batch'), 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (runId: string) => api.deleteLiteratureRun(libraryId, runId),
    onSuccess: (_result, runId) => {
      if (activeRunId === runId) setActiveRunId(null);
      void queryClient.invalidateQueries({ queryKey: ['literature-runs', libraryId] });
      toast(tr('检索历史已永久删除', 'Search history permanently deleted'), 'ok');
    },
    onError: () => toast(tr('删除失败；运行中的检索需先取消', 'Delete failed; cancel an active run first'), 'error'),
  });
  const cancelMutation = useMutation({
    mutationFn: (runId: string) => api.cancelLiteratureRun(libraryId, runId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['literature-run', libraryId, selectedRunId] }),
    onError: () => toast(tr('取消失败', 'Could not cancel the run'), 'error'),
  });

  const hits = hitsQuery.data?.items ?? [];
  const selectedHits = hits.filter((hit) => selected.has(hit.id));
  const candidateIds = selectedHits.filter((hit) => hit.status === 'candidate').map((hit) => hit.id);
  const oaCandidateIds = selectedHits
    .filter((hit) => hit.status === 'candidate' && oaByHit.get(hit.id)?.status !== 'ready')
    .map((hit) => hit.id);
  const extensionHits = eligibleExtensionHits(hits, selected, oaByHit);
  const sourceOptions = runQuery.data?.source_attempts.map((attempt) => attempt.source) ?? [];
  const allVisibleSelected = hits.length > 0 && hits.every((hit) => selected.has(hit.id));

  const toggleHit = (hitId: string) => setSelected((old) => {
    const next = new Set(old);
    if (next.has(hitId)) next.delete(hitId);
    else next.add(hitId);
    return next;
  });
  const confirmDelete = (run: LiteratureSearchRun) => {
    if (window.confirm(tr(
      '永久删除本次检索及其候选结果？此操作不可恢复，已经入库的论文和 PDF 不受影响。',
      'Permanently delete this run and its candidates? Imported papers and PDFs are kept.',
    ))) deleteMutation.mutate(run.id);
  };

  return (
    <div className="literature-discovery">
      <header className="literature-discovery-header">
        <div className="literature-discovery-heading">
          <div className="row gap8">
            <Icon name="compass" size={17} />
            <h2>{tr('多源文献发现', 'Literature discovery')}</h2>
            {readOnly && <span className="pill sm">{tr('只读', 'Read only')}</span>}
          </div>
          <p>{tr('检索结果先进入待筛选池；只有入库后的 PDF 才会解析、向量化并进入 AI 证据链。', 'Results enter a review pool first. PDF parsing and indexing start only after import.')}</p>
        </div>
        <button className="btn btn-ghost sm" onClick={() => setHistoryOpen(true)}>
          <Icon name="clock" size={14} />
          {tr(`检索历史${runsQuery.data ? ` · ${runsQuery.data.total}` : ''}`, `History${runsQuery.data ? ` · ${runsQuery.data.total}` : ''}`)}
        </button>
      </header>

      {!readOnly && (
        <section className="literature-search-launcher">
          <label className="literature-search-topic">
            <span>{tr('检索主题补充', 'Topic override')}</span>
            <input
              className="input"
              value={topic}
              onChange={(event) => setTopic(event.target.value)}
              placeholder={tr('留空时使用文献库名称与方向定义', 'Uses the library name and definition when empty')}
              maxLength={4000}
            />
          </label>
          <label>
            <span>{tr('返回数量', 'Results')}</span>
            <input className="input" type="number" min={1} max={200} value={requestedCount} onChange={(event) => setRequestedCount(Math.max(1, Math.min(200, Number(event.target.value) || 1)))} />
          </label>
          <label>
            <span>{tr('起始年份', 'Start year')}</span>
            <input className="input" type="number" min={1800} max={CURRENT_YEAR} value={startYear} onChange={(event) => setStartYear(Math.max(1800, Math.min(CURRENT_YEAR, Number(event.target.value) || CURRENT_YEAR)))} />
          </label>
          <button className="btn btn-primary" disabled={startMutation.isPending} onClick={() => startMutation.mutate()}>
            <Icon name={startMutation.isPending ? 'refresh' : 'search'} size={15} style={startMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
            {startMutation.isPending ? tr('正在创建', 'Starting') : tr('开始检索', 'Start search')}
          </button>
        </section>
      )}

      {runQuery.data && (
        <section className="literature-run-status">
          <div className="literature-run-summary">
            <div>
              <span className={`literature-status-dot is-${runQuery.data.status}`} />
              <strong>{tr(...(STATUS_LABELS[runQuery.data.status] ?? [runQuery.data.status, runQuery.data.status]))}</strong>
              <span>{progressMessage(runQuery.data)}</span>
            </div>
            <div className="row gap8">
              <span>{runQuery.data.requested_count} {tr('篇', 'papers')}</span>
              <span>{runQuery.data.start_year ?? '—'}–{runQuery.data.end_year ?? '—'}</span>
              {!readOnly && !TERMINAL.has(runQuery.data.status) && (
                <button className="btn btn-ghost sm" disabled={cancelMutation.isPending} onClick={() => cancelMutation.mutate(runQuery.data!.id)}>
                  {tr('取消', 'Cancel')}
                </button>
              )}
            </div>
          </div>
          <div className="bar"><i style={{ width: `${progressPercent(runQuery.data)}%` }} /></div>
          <div className="literature-source-progress">
            {runQuery.data.source_attempts.map((attempt) => (
              <span key={attempt.id} className={`literature-source-chip is-${attempt.status}`} title={attempt.error_detail ?? attempt.query ?? undefined}>
                <i />{sourceName(attempt.source)}
                <b>{attempt.accepted_count}</b>
              </span>
            ))}
          </div>
          {runQuery.data.error_summary && <div className="literature-run-error">{runQuery.data.error_summary}</div>}
        </section>
      )}

      {selectedRunId ? (
        <>
          <section className="literature-result-toolbar">
            <label className="literature-filter-search">
              <Icon name="search" size={14} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={tr('搜索标题、摘要或 DOI', 'Search title, abstract, or DOI')} />
            </label>
            <select className="input" value={source} onChange={(event) => setSource(event.target.value)} aria-label={tr('来源', 'Source')}>
              <option value="">{tr('全部来源', 'All sources')}</option>
              {sourceOptions.map((value) => <option key={value} value={value}>{sourceName(value)}</option>)}
            </select>
            <select className="input" value={hitStatus} onChange={(event) => setHitStatus(event.target.value as typeof hitStatus)} aria-label={tr('入库状态', 'Import status')}>
              <option value="">{tr('全部状态', 'All statuses')}</option>
              <option value="candidate">{tr('待筛选', 'Candidate')}</option>
              <option value="promoted">{tr('已入库', 'Imported')}</option>
            </select>
            <input className="input literature-year-input" type="number" min={1800} max={CURRENT_YEAR} value={yearFrom} placeholder={tr('起始年', 'From')} onChange={(event) => setYearFrom(event.target.value ? Number(event.target.value) : '')} />
            <input className="input literature-year-input" type="number" min={1800} max={CURRENT_YEAR} value={yearTo} placeholder={tr('截止年', 'To')} onChange={(event) => setYearTo(event.target.value ? Number(event.target.value) : '')} />
            <select className="input" value={sort} onChange={(event) => setSort(event.target.value as HitSort)} aria-label={tr('排序', 'Sort')}>
              <option value="relevance">{tr('相关度排序', 'Relevance')}</option>
              <option value="novelty">{tr('新颖度排序', 'Novelty')}</option>
              <option value="impact">{tr('影响力排序', 'Impact')}</option>
              <option value="recent">{tr('最近发现', 'Recent')}</option>
              <option value="title">{tr('标题排序', 'Title')}</option>
            </select>
          </section>

          {!readOnly && (
            <section className="literature-batch-bar">
              <label className="row gap8">
                <input type="checkbox" checked={allVisibleSelected} onChange={() => setSelected(allVisibleSelected ? new Set() : new Set(hits.map((hit) => hit.id)))} />
                <span>{tr(`已选 ${selected.size} 篇`, `${selected.size} selected`)}</span>
              </label>
              <div className="row gap8">
                <button className="btn btn-soft sm" disabled={!selectedHits.length || translateMutation.isPending} onClick={() => translateMutation.mutate(selectedHits.map((hit) => hit.id))}>
                  <Icon name={translateMutation.isPending ? 'refresh' : 'chat'} size={13} style={translateMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
                  {tr('译为中文', 'Translate')}
                </button>
                <button className="btn btn-soft sm" disabled={!oaCandidateIds.length || cacheMutation.isPending} onClick={() => cacheMutation.mutate(oaCandidateIds)}>
                  <Icon name="download" size={13} />
                  {tr(`缓存 OA PDF · ${oaCandidateIds.length}`, `Cache OA PDFs · ${oaCandidateIds.length}`)}
                </button>
                <button className="btn btn-soft sm" disabled={!extensionHits.length || extensionMutation.isPending} title={!extensionHits.length && selected.size ? tr('只有已入库且尚无缓存 PDF 的文献可以推送扩展', 'Only imported papers without a cached PDF can be sent') : undefined} onClick={() => extensionMutation.mutate(extensionHits)}>
                  <Icon name="share" size={13} />
                  {tr(`推送扩展 · ${extensionHits.length}`, `Send to extension · ${extensionHits.length}`)}
                </button>
                <button className="btn btn-primary sm" disabled={!candidateIds.length || promoteMutation.isPending} onClick={() => promoteMutation.mutate(candidateIds)}>
                  <Icon name="plus" size={13} />
                  {tr(`筛选入库 · ${candidateIds.length}`, `Import · ${candidateIds.length}`)}
                </button>
              </div>
            </section>
          )}

          <div className="literature-results-meta">
            <span>{tr(`共 ${hitsQuery.data?.total ?? 0} 篇结果`, `${hitsQuery.data?.total ?? 0} results`)}</span>
            <span>{tr('评分口径：相关性 45% · 证据质量 20% · 影响力 15% · 创新 10% · 时效性 10%', 'Scoring: relevance 45%, evidence 20%, impact 15%, novelty 10%, recency 10%')}</span>
          </div>

          {hitsQuery.isLoading ? (
            <div className="literature-result-list">{Array.from({ length: 4 }, (_, index) => <div className="skel" style={{ height: 190 }} key={index} />)}</div>
          ) : hitsQuery.isError ? (
            <EmptyState icon="x" title={tr('无法加载检索结果', 'Could not load results')} desc={tr('请检查后端连接后重试。', 'Check the backend connection and retry.')} action={<button className="btn btn-soft sm" onClick={() => void hitsQuery.refetch()}>{tr('重试', 'Retry')}</button>} />
          ) : !hits.length ? (
            <EmptyState icon="search" title={tr('没有匹配结果', 'No matching papers')} desc={tr('调整搜索、年份、来源或状态筛选条件。', 'Adjust the search, year, source, or status filters.')} />
          ) : (
            <div className="literature-result-list">
              {hits.map((hit) => {
                const oa = oaByHit.get(hit.id);
                const translation = translations.get(hit.id);
                const translated = translatedFields(translation);
                const translating = translation?.status === 'queued' || translation?.status === 'running';
                const overall = percentage(hit.scores?.overall);
                const labels = venueLabels(hit);
                const reasons = translated?.inclusion_rationale ?? scoreReasons(hit);
                return (
                  <article className={`literature-result${selected.has(hit.id) ? ' is-selected' : ''}`} key={hit.id}>
                    {!readOnly && <input className="literature-result-check" type="checkbox" checked={selected.has(hit.id)} onChange={() => toggleHit(hit.id)} aria-label={tr('选择文献', 'Select paper')} />}
                    <div className="literature-result-main">
                      <div className="literature-result-kicker">
                        <span className="literature-source-badge">{sourceName(hit.source)}</span>
                        {hit.year && <span>{hit.year}</span>}
                        <span>{hit.venue || tr('期刊未知', 'Unknown venue')}</span>
                        {labels.map((label) => <span className="pill sm" key={label}>{label}</span>)}
                        {hit.citation_count !== null && <span>{tr(`引用 ${hit.citation_count}`, `${hit.citation_count} citations`)}</span>}
                      </div>
                      <h3>{translated?.title || hit.title}</h3>
                      <div className="literature-result-authors">{authorNames(hit)}</div>
                      <p className="literature-result-abstract">{translated?.abstract || hit.abstract || tr('该来源未提供摘要。', 'No abstract supplied by this source.')}</p>
                      <div className="literature-result-links">
                        {hit.doi && <a href={`https://doi.org/${hit.doi}`} target="_blank" rel="noreferrer">DOI {hit.doi}</a>}
                        {hit.url && <a href={hit.url} target="_blank" rel="noreferrer">{tr('来源页面', 'Source page')}</a>}
                        {hit.pdf_url && <a href={hit.pdf_url} target="_blank" rel="noreferrer">PDF</a>}
                        <span className={`literature-asset-state is-${oa?.status ?? (hit.pdf_url ? 'available' : 'missing')}`}>
                          {oa?.status === 'ready' ? tr('OA 已缓存', 'OA cached') : oa?.status === 'failed' ? tr('OA 缓存失败', 'OA cache failed') : hit.pdf_url ? tr('发现开放 PDF', 'OA PDF found') : tr('未发现 PDF', 'No PDF found')}
                        </span>
                        {hit.status === 'promoted' && <span className="literature-imported"><Icon name="check" size={11} />{tr('已入库', 'Imported')}</span>}
                      </div>
                      {!!reasons.length && (
                        <div className="literature-rationale">
                          <strong>{tr('入选依据', 'Why it was selected')}</strong>
                          <span>{reasons.join(' · ')}</span>
                        </div>
                      )}
                    </div>
                    <aside className="literature-score-panel">
                      <div className="literature-overall-score"><strong>{overall ?? '—'}</strong><span>{tr('综合分', 'Overall')}</span></div>
                      {SCORE_DIMENSIONS.map(([key, zh, en]) => {
                        const value = percentage(hit.scores?.[key]);
                        return <div className="literature-score-row" key={key}><span>{tr(zh, en)}</span><i><b style={{ width: `${value ?? 0}%` }} /></i><em>{value ?? '—'}</em></div>;
                      })}
                      {!readOnly && (
                        <button className="btn btn-ghost sm" disabled={translating || translateMutation.isPending} onClick={() => translateMutation.mutate([hit.id])}>
                          <Icon name={translating ? 'refresh' : 'chat'} size={12} style={translating ? { animation: 'spin 1s linear infinite' } : undefined} />
                          {translating ? tr('翻译中', 'Translating') : translated ? tr('重新翻译', 'Translate again') : tr('译为中文', 'Translate')}
                        </button>
                      )}
                      {translation?.status === 'failed' && <small>{translation.error_code || tr('翻译失败', 'Translation failed')}</small>}
                    </aside>
                  </article>
                );
              })}
            </div>
          )}

          {(hitsQuery.data?.total ?? 0) > PAGE_SIZE && (
            <div className="literature-pagination">
              <button className="btn btn-ghost sm" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>{tr('上一页', 'Previous')}</button>
              <span>{page} / {Math.ceil((hitsQuery.data?.total ?? 0) / PAGE_SIZE)}</span>
              <button className="btn btn-ghost sm" disabled={page * PAGE_SIZE >= (hitsQuery.data?.total ?? 0)} onClick={() => setPage((value) => value + 1)}>{tr('下一页', 'Next')}</button>
            </div>
          )}
        </>
      ) : (
        <EmptyState icon="compass" title={tr('还没有检索记录', 'No searches yet')} desc={readOnly ? tr('该文献库尚未运行文献发现。', 'No discovery run has been created for this library.') : tr('设置返回数量和起始年份，开始第一次多源检索。', 'Choose a result count and start year to begin.')} />
      )}

      <Modal open={historyOpen} onClose={() => setHistoryOpen(false)} title={tr('全部检索历史', 'Search history')} sub={libraryQuery.data?.name} width={780}>
        <div className="literature-history">
          {runs.map((run) => (
            <div className={`literature-history-row${selectedRunId === run.id ? ' is-active' : ''}`} key={run.id}>
              <button onClick={() => { setActiveRunId(run.id); setHistoryOpen(false); }}>
                <span><strong>{run.topic}</strong><small>{new Date(run.created_at).toLocaleString()} · {run.trigger === 'scheduled' ? tr('每日增量', 'Scheduled') : tr('手动检索', 'Manual')} · {run.requested_count} {tr('篇', 'papers')} · {run.start_year ?? '—'}–{run.end_year ?? '—'}</small></span>
                <b>{tr(...(STATUS_LABELS[run.status] ?? [run.status, run.status]))}</b>
              </button>
              {!readOnly && TERMINAL.has(run.status) && <button className="icon-btn danger" title={tr('永久删除', 'Delete permanently')} onClick={() => confirmDelete(run)}><Icon name="trash" size={14} /></button>}
            </div>
          ))}
          {!runs.length && <div className="empty">{tr('暂无检索历史', 'No search history')}</div>}
        </div>
      </Modal>
    </div>
  );
}
