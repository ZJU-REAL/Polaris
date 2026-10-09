import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
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
  queued: ['排队中', 'Queued'],
  running: ['检索中', 'Searching'],
  completed: ['已完成', 'Completed'],
  partial: ['部分完成', 'Partly done'],
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
  ['novelty', '新颖性', 'Novelty'],
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
    values.push(tr(
      `中科院 ${String(casZone).includes('区') ? casZone : `${casZone}区`}`,
      `CAS zone ${String(casZone).replace('区', '')}`,
    ));
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
  if (phase === 'queued') return tr('排队中…', 'Waiting to start…');
  if (phase === 'retrieving') {
    const source = typeof progress.source === 'string' ? sourceName(progress.source) : null;
    const done = numeric(progress.query_completed);
    const total = numeric(progress.query_total);
    const suffix = done !== null && total !== null ? ` · ${done}/${total}` : '';
    return source
      ? tr(`正在检索 ${source}${suffix}`, `Searching ${source}${suffix}`)
      : tr('正在检索各来源', 'Searching sources');
  }
  if (phase === 'ranking') {
    const count = numeric(progress.pending_rerank) ?? numeric(progress.deduplicated);
    return count !== null
      ? tr(`正在排序 ${count} 篇候选论文`, `Ranking ${count} candidates`)
      : tr('正在排序候选论文', 'Ranking candidates');
  }
  if (run.status === 'completed') return tr('检索完成', 'Search complete');
  if (run.status === 'partial') return tr('部分来源未能检索，其余结果已保存', 'Some sources failed. The other results were saved.');
  if (run.status === 'failed') return tr('检索失败，请查看下方各来源的状态', 'Search failed. Check each source below.');
  if (run.status === 'cancelled') return tr('检索已取消', 'Search cancelled');
  return tr('正在准备…', 'Preparing…');
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
}: {
  libraryId: string;
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
      toast(tr('已开始检索', 'Search started'), 'ok');
    },
    onError: (error) => toast(error instanceof Error ? error.message : tr('无法开始检索', 'Couldn’t start the search'), 'error'),
  });
  const translateMutation = useMutation({
    mutationFn: (hitIds: string[]) => api.translateLiteratureHits(libraryId, selectedRunId!, hitIds),
    onSuccess: (rows) => {
      setTranslationIds((old) => new Set([...old, ...rows.map((row) => row.hit_id)]));
      rows.forEach((row) => queryClient.setQueryData(
        ['literature-translation', libraryId, selectedRunId, row.hit_id],
        row,
      ));
      toast(tr('正在翻译…', 'Translating…'), 'ok');
    },
    onError: (error) => toast(error instanceof Error ? error.message : tr('无法翻译', 'Couldn’t translate'), 'error'),
  });
  const promoteMutation = useMutation({
    mutationFn: (hitIds: string[]) => api.promoteLiteratureHits(libraryId, selectedRunId!, hitIds),
    onSuccess: (items) => {
      setSelected(new Set());
      void queryClient.invalidateQueries({ queryKey: ['literature-hits', libraryId, selectedRunId] });
      void queryClient.invalidateQueries({ queryKey: ['library-papers', libraryId] });
      toast(tr(`已将 ${items.length} 篇加入本库`, `Added ${items.length} papers to the library`), 'ok');
    },
    onError: () => toast(tr('无法加入本库', 'Couldn’t add the papers'), 'error'),
  });
  const cacheMutation = useMutation({
    mutationFn: (hitIds: string[]) => api.cacheLiteratureOaPdfs(libraryId, selectedRunId!, hitIds),
    onSuccess: (items) => {
      void queryClient.invalidateQueries({ queryKey: ['literature-oa-cache', libraryId, selectedRunId] });
      const ready = items.filter((item) => item.status === 'ready').length;
      toast(tr(`已下载 ${ready}/${items.length} 篇 PDF`, `Downloaded ${ready} of ${items.length} PDFs`), 'ok');
    },
    onError: () => toast(tr('无法下载 PDF', 'Couldn’t download the PDFs'), 'error'),
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
          ? tr('所选论文都已有 PDF，无需下载', 'All selected papers already have PDFs')
          : acknowledged
          ? tr(`已发送 ${batch.item_count} 篇到浏览器扩展${skipped ? `，${skipped} 篇已有 PDF` : ''}`, `Sent ${batch.item_count} papers to the browser extension${skipped ? `. ${skipped} already had PDFs.` : ''}`)
          : tr('已保存，浏览器扩展连接后会领取', 'Saved. The browser extension will pick it up when it connects.'),
        dispatchedCount === 0 || acknowledged ? 'ok' : 'info',
      );
    },
    onError: () => toast(tr('无法发送到浏览器扩展', 'Couldn’t send to the browser extension'), 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (runId: string) => api.deleteLiteratureRun(libraryId, runId),
    onSuccess: (_result, runId) => {
      if (activeRunId === runId) setActiveRunId(null);
      void queryClient.invalidateQueries({ queryKey: ['literature-runs', libraryId] });
      toast(tr('已删除检索记录', 'Search deleted'), 'ok');
    },
    onError: () => toast(tr('无法删除，请先取消正在运行的检索', 'Couldn’t delete. Cancel the running search first.'), 'error'),
  });
  const cancelMutation = useMutation({
    mutationFn: (runId: string) => api.cancelLiteratureRun(libraryId, runId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['literature-run', libraryId, selectedRunId] }),
    onError: () => toast(tr('无法取消检索', 'Couldn’t cancel the search'), 'error'),
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
  const [pendingDeleteRun, setPendingDeleteRun] = useState<LiteratureSearchRun | null>(null);
  const confirmDelete = (run: LiteratureSearchRun) => setPendingDeleteRun(run);

  return (
    <div className="literature-discovery">
      <header className="literature-discovery-header">
        <div className="literature-discovery-heading">
          <div className="row gap8">
            <h2>{tr('发现文献', 'Discover papers')}</h2>
            
          </div>
          <p>{tr('从多个来源检索论文，挑选后加入本库。', 'Search several sources, then add the papers you choose.')}</p>
        </div>
        <button className="btn btn-ghost sm" onClick={() => setHistoryOpen(true)}>
          <Icon name="clock" size={14} />
          {tr(`检索历史${runsQuery.data ? ` · ${runsQuery.data.total}` : ''}`, `History${runsQuery.data ? ` · ${runsQuery.data.total}` : ''}`)}
        </button>
      </header>

      <section className="literature-search-launcher">
        <label className="literature-search-topic">
          <span>{tr('检索主题', 'Topic')}</span>
          <input
            className="input"
            value={topic}
            onChange={(event) => setTopic(event.target.value)}
            placeholder={tr('留空则使用文献库的方向说明', 'Leave empty to use the library’s scope')}
            maxLength={4000}
          />
        </label>
        <label>
          <span>{tr('结果数', 'Results')}</span>
          <input className="input" type="number" min={1} max={200} value={requestedCount} onChange={(event) => setRequestedCount(Math.max(1, Math.min(200, Number(event.target.value) || 1)))} />
        </label>
        <label>
          <span>{tr('起始年份', 'From year')}</span>
          <input className="input" type="number" min={1800} max={CURRENT_YEAR} value={startYear} onChange={(event) => setStartYear(Math.max(1800, Math.min(CURRENT_YEAR, Number(event.target.value) || CURRENT_YEAR)))} />
        </label>
        <button className="btn btn-primary" disabled={startMutation.isPending} onClick={() => startMutation.mutate()}>
          <Icon name={startMutation.isPending ? 'refresh' : 'search'} size={15} style={startMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
          {startMutation.isPending ? tr('正在开始…', 'Starting…') : tr('开始检索', 'Start search')}
        </button>
      </section>

      {runQuery.data && (
        <section className="literature-run-status">
          <div className="literature-run-summary">
            <div>
              <span className={`literature-status-dot is-${runQuery.data.status}`} />
              <strong>{tr(...(STATUS_LABELS[runQuery.data.status] ?? [runQuery.data.status, runQuery.data.status]))}</strong>
              <span>{progressMessage(runQuery.data)}</span>
            </div>
            <div className="row gap8">
              <span>{tr(`${runQuery.data.requested_count} 篇`, `${runQuery.data.requested_count} papers`)}</span>
              <span>{runQuery.data.start_year ?? '—'}–{runQuery.data.end_year ?? '—'}</span>
              {!TERMINAL.has(runQuery.data.status) && (
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
            <select className="input" value={hitStatus} onChange={(event) => setHitStatus(event.target.value as typeof hitStatus)} aria-label={tr('状态', 'Status')}>
              <option value="">{tr('全部状态', 'All statuses')}</option>
              <option value="candidate">{tr('未加入', 'Not added')}</option>
              <option value="promoted">{tr('已加入', 'Added')}</option>
            </select>
            <input className="input literature-year-input" type="number" min={1800} max={CURRENT_YEAR} value={yearFrom} placeholder={tr('起始年', 'From')} onChange={(event) => setYearFrom(event.target.value ? Number(event.target.value) : '')} />
            <input className="input literature-year-input" type="number" min={1800} max={CURRENT_YEAR} value={yearTo} placeholder={tr('截止年', 'To')} onChange={(event) => setYearTo(event.target.value ? Number(event.target.value) : '')} />
            <select className="input" value={sort} onChange={(event) => setSort(event.target.value as HitSort)} aria-label={tr('排序', 'Sort')}>
              <option value="relevance">{tr('按相关度', 'Relevance')}</option>
              <option value="novelty">{tr('按新颖性', 'Novelty')}</option>
              <option value="impact">{tr('按影响力', 'Impact')}</option>
              <option value="recent">{tr('最新发现', 'Newest')}</option>
              <option value="title">{tr('按标题', 'Title')}</option>
            </select>
          </section>

          <section className="literature-batch-bar">
            <label className="row gap8">
              <input type="checkbox" checked={allVisibleSelected} onChange={() => setSelected(allVisibleSelected ? new Set() : new Set(hits.map((hit) => hit.id)))} />
              <span>{tr(`已选 ${selected.size} 篇`, `${selected.size} selected`)}</span>
            </label>
            <div className="row gap8 wrap">
              <button className="btn btn-soft sm" disabled={!selectedHits.length || translateMutation.isPending} onClick={() => translateMutation.mutate(selectedHits.map((hit) => hit.id))}>
                <Icon name={translateMutation.isPending ? 'refresh' : 'chat'} size={13} style={translateMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
                {tr('译为中文', 'Translate to Chinese')}
              </button>
              <button className="btn btn-soft sm" disabled={!oaCandidateIds.length || cacheMutation.isPending} title={tr('仅限开放获取的论文', 'Open-access papers only')} onClick={() => cacheMutation.mutate(oaCandidateIds)}>
                <Icon name="download" size={13} />
                {tr(`下载 PDF · ${oaCandidateIds.length}`, `Download PDFs · ${oaCandidateIds.length}`)}
              </button>
              <button className="btn btn-soft sm" disabled={!extensionHits.length || extensionMutation.isPending} title={!extensionHits.length && selected.size ? tr('只能发送已加入本库、且还没有 PDF 的论文', 'Only papers already added and without a PDF can be sent') : undefined} onClick={() => extensionMutation.mutate(extensionHits)}>
                <Icon name="share" size={13} />
                {tr(`发送到浏览器扩展 · ${extensionHits.length}`, `Send to browser extension · ${extensionHits.length}`)}
              </button>
              <button className="btn btn-primary sm" disabled={!candidateIds.length || promoteMutation.isPending} onClick={() => promoteMutation.mutate(candidateIds)}>
                <Icon name="plus" size={13} />
                {tr(`加入本库 · ${candidateIds.length}`, `Add to library · ${candidateIds.length}`)}
              </button>
            </div>
          </section>

          <div className="literature-results-meta">
            <span>{tr(`共 ${hitsQuery.data?.total ?? 0} 篇`, `${hitsQuery.data?.total ?? 0} results`)}</span>
          </div>

          {hitsQuery.isLoading ? (
            <div className="literature-result-list">{Array.from({ length: 4 }, (_, index) => <div className="skel" style={{ height: 190 }} key={index} />)}</div>
          ) : hitsQuery.isError ? (
            <EmptyState icon="x" title={tr('无法加载检索结果', 'Couldn’t load results')} desc={tr('请确认本机引擎正在运行。', 'Check that the local engine is running.')} action={<button className="btn btn-soft sm" onClick={() => void hitsQuery.refetch()}>{tr('重试', 'Retry')}</button>} />
          ) : !hits.length ? (
            <EmptyState icon="search" title={tr('没有匹配的论文', 'No matching papers')} desc={tr('试试调整筛选条件。', 'Try adjusting the filters.')} />
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
                    {<input className="literature-result-check" type="checkbox" checked={selected.has(hit.id)} onChange={() => toggleHit(hit.id)} aria-label={tr('选择论文', 'Select paper')} />}
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
                      <p className="literature-result-abstract">{translated?.abstract || hit.abstract || tr('没有摘要', 'No abstract')}</p>
                      <div className="literature-result-links">
                        {hit.doi && <a href={`https://doi.org/${hit.doi}`} target="_blank" rel="noreferrer">DOI {hit.doi}</a>}
                        {hit.url && <a href={hit.url} target="_blank" rel="noreferrer">{tr('来源页面', 'Source page')}</a>}
                        {hit.pdf_url && <a href={hit.pdf_url} target="_blank" rel="noreferrer">PDF</a>}
                        <span className={`literature-asset-state is-${oa?.status ?? (hit.pdf_url ? 'available' : 'missing')}`}>
                          {oa?.status === 'ready' ? tr('PDF 已下载', 'PDF downloaded') : oa?.status === 'failed' ? tr('PDF 下载失败', 'PDF download failed') : hit.pdf_url ? tr('有开放 PDF', 'Open PDF available') : tr('没有 PDF', 'No PDF')}
                        </span>
                        {hit.status === 'promoted' && <span className="literature-imported"><Icon name="check" size={11} />{tr('已加入', 'Added')}</span>}
                      </div>
                      {!!reasons.length && (
                        <div className="literature-rationale">
                          <strong>{tr('推荐理由', 'Why it fits')}</strong>
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
                      <button className="btn btn-ghost sm" disabled={translating || translateMutation.isPending} onClick={() => translateMutation.mutate([hit.id])}>
                        <Icon name={translating ? 'refresh' : 'chat'} size={12} style={translating ? { animation: 'spin 1s linear infinite' } : undefined} />
                        {translating ? tr('翻译中…', 'Translating…') : translated ? tr('重新翻译', 'Translate again') : tr('译为中文', 'Translate to Chinese')}
                      </button>
                      {translation?.status === 'failed' && <small>{tr('无法翻译', 'Couldn’t translate')}</small>}
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
        <EmptyState icon="compass" title={tr('还没有检索记录', 'No searches yet')} desc={tr('设置结果数和起始年份后开始检索。', 'Set the result count and start year, then start a search.')} />
      )}

      <Modal open={historyOpen} onClose={() => setHistoryOpen(false)} title={tr('检索历史', 'Search history')} sub={libraryQuery.data?.name} width={780}>
        <div className="literature-history">
          {runs.map((run) => (
            <div className={`literature-history-row${selectedRunId === run.id ? ' is-active' : ''}`} key={run.id}>
              <button onClick={() => { setActiveRunId(run.id); setHistoryOpen(false); }}>
                <span><strong>{run.topic}</strong><small>{new Date(run.created_at).toLocaleString()} · {run.trigger === 'scheduled' ? tr('每日自动', 'Daily') : tr('手动', 'Manual')} · {tr(`${run.requested_count} 篇`, `${run.requested_count} papers`)} · {run.start_year ?? '—'}–{run.end_year ?? '—'}</small></span>
                <b>{tr(...(STATUS_LABELS[run.status] ?? [run.status, run.status]))}</b>
              </button>
              {TERMINAL.has(run.status) && <button className="icon-btn danger" title={tr('删除', 'Delete')} onClick={() => confirmDelete(run)}><Icon name="trash" size={14} /></button>}
            </div>
          ))}
          {!runs.length && <div className="empty">{tr('还没有检索记录', 'No searches yet')}</div>}
        </div>
      </Modal>
      <ConfirmModal
        open={!!pendingDeleteRun}
        onClose={() => setPendingDeleteRun(null)}
        title={tr('删除这次检索？', 'Delete this search?')}
        message={tr('其中的候选结果会被删除，已加入本库的论文不受影响。', 'Its candidates are removed. Papers already added stay in the library.')}
        confirmText={tr('删除', 'Delete')}
        danger
        onConfirm={() => {
          if (pendingDeleteRun) deleteMutation.mutate(pendingDeleteRun.id);
          setPendingDeleteRun(null);
        }}
      />
    </div>
  );
}
