import { useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import {
  api,
  ApiError,
  type AnchorPaper,
  type DirectionLibraryDetail,
  type IngestStart,
  type IngestState,
  type IngestTimeRange,
  type LiteratureSearchRun,
  type VoyageRead,
} from '../../lib/api';
import { errorText } from '../../lib/errors';
import { parseApiDate } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { researchApi } from '../../lib/researchApi';
import { researchKeys } from '../deepresearch/hooks';
import type { Direction } from '../deepresearch/directions';
import { TaskStatus } from '../voyages/shared/StatusDot';
import { taskTitle } from '../voyages/shared/taskText';
import { LiteratureDiscoveryPanel } from '../libraries/LiteratureDiscoveryPage';
import { AddPapersModal, type ImportMethod } from './AddPapersModal';
import { AnchorEditor, KnobRange } from './findParts';

/* ============================================================
   文献库的「找论文」页签。原来的「发现文献」和「检索与同步」两套找论文的办法
   合成一页、一份记录：

     保持更新   每天从新论文里挑相关的加入（上次 / 下次 / 立即更新）；
                研究建立的文献库还写明当时怎么检索的，可以回研究页调整范围重新检索
     找新论文   关键词检索（先列出来我挑 / 相关的直接加入）· 按编号或 BibTeX ·
                从 Zotero 导入 · 顺着引用找
     记录       两种检索和每次更新放在同一张列表里，最新的在上
   ============================================================ */

type Method = 'keywords' | 'ids' | 'zotero' | 'citations';
type Handling = 'pick' | 'auto';

const CURRENT_YEAR = new Date().getFullYear();
const LITERATURE_KINDS = new Set(['wiki_bootstrap', 'wiki_ingest']);

const DISCOVER_TONE: Record<string, string> = {
  queued: 'queued',
  running: 'running',
  completed: 'completed',
  partial: 'completed',
  failed: 'failed',
  cancelled: 'cancelled',
};

function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = parseApiDate(iso);
  return d.toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

interface HistoryItem {
  key: string;
  at: string;
  title: string;
  detail: string;
  status: ReactNode;
  /** 打开：任务去任务页，挑选式检索在本页下方展开结果 */
  onOpen: () => void;
  testId: string;
}

export function FindPapersTab({
  lib,
  state,
  stateError,
  direction,
  onOpenPaper,
  onOpenSettings,
}: {
  lib: DirectionLibraryDetail;
  state: IngestState | undefined;
  stateError: boolean;
  direction: Direction | null;
  onOpenPaper: (id: string) => void;
  onOpenSettings: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const libraryId = lib.id;
  const run = direction?.run && direction.run.library_id === libraryId ? direction.run : null;

  const [method, setMethod] = useState<Method>('keywords');
  const [handling, setHandling] = useState<Handling>('pick');
  const [keywords, setKeywords] = useState('');
  const [resultCount, setResultCount] = useState(50);
  const [startYear, setStartYear] = useState(CURRENT_YEAR - 5);
  const [timeRange, setTimeRange] = useState<IngestTimeRange>('6m');
  const [threshold, setThreshold] = useState(0.8);
  const [maxPapers, setMaxPapers] = useState(150);
  const [compileTopN, setCompileTopN] = useState(50);
  const [unlimited, setUnlimited] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [hops, setHops] = useState<1 | 2 | 3>(1);
  const [addMethod, setAddMethod] = useState<ImportMethod | null>(null);
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [queriesOpen, setQueriesOpen] = useState(false);

  const includeKeywords = lib.definition?.keywords?.include ?? [];
  const anchors = lib.definition?.anchor_papers ?? [];
  const running = !!state?.running_voyage_id;

  // —— 研究建立的文献库：当时的检索 ——
  const runQuery = useQuery({
    queryKey: researchKeys.run(run?.id ?? ''),
    queryFn: () => researchApi.getRun(run!.id),
    enabled: !!run,
    retry: false,
  });
  const survey = runQuery.data?.survey;

  // —— 记录：这个库的任务 + 挑选式检索 ——
  const voyagesQuery = useQuery({
    queryKey: ['voyages', 'all'],
    queryFn: () => api.listVoyages(),
    retry: false,
    refetchInterval: running ? 5_000 : 30_000,
  });
  const runsQuery = useQuery({
    queryKey: ['literature-runs', libraryId],
    queryFn: () => api.listLiteratureRuns(libraryId, { size: 50 }),
    retry: false,
    refetchInterval: (q) => (q.state.data?.items.some((r) => r.status === 'running' || r.status === 'queued') ? 3_000 : 30_000),
  });

  const history = useMemo<HistoryItem[]>(() => {
    const items: HistoryItem[] = [];
    for (const v of (voyagesQuery.data ?? []) as VoyageRead[]) {
      // 只列找论文的任务（检索建库 / 每日更新）；同一个库上的「寻找假设」等不算
      if (v.library_id !== libraryId || !LITERATURE_KINDS.has(v.kind)) continue;
      items.push({
        key: `v:${v.id}`,
        at: v.created_at,
        title: taskTitle(v),
        detail: v.kind === 'wiki_ingest' ? tr('每日更新', 'Daily update') : tr('检索并直接加入相关论文', 'Searched and added relevant papers'),
        status: <TaskStatus status={v.status} />,
        onOpen: () => navigate(`/voyages/${v.id}`),
        testId: 'find-history-task',
      });
    }
    for (const r of (runsQuery.data?.items ?? []) as LiteratureSearchRun[]) {
      items.push({
        key: `r:${r.id}`,
        at: r.created_at,
        title: tr(`检索「${r.topic.slice(0, 60)}」`, `Search “${r.topic.slice(0, 60)}”`),
        detail:
          r.trigger === 'scheduled'
            ? tr('每日自动检索，挑选后加入', 'Daily search, you pick what to add')
            : tr(`列出最多 ${r.requested_count} 篇，挑选后加入`, `Lists up to ${r.requested_count}, you pick what to add`),
        status: <TaskStatus status={DISCOVER_TONE[r.status] ?? r.status} />,
        onOpen: () => setOpenRunId(r.id),
        testId: 'find-history-search',
      });
    }
    return items.sort((a, b) => b.at.localeCompare(a.at));
  }, [voyagesQuery.data, runsQuery.data, libraryId, navigate]);

  const invalidateState = () => {
    void queryClient.invalidateQueries({ queryKey: ['ingest-state', libraryId] });
    void queryClient.invalidateQueries({ queryKey: ['voyages', 'all'] });
  };

  const ingestMutation = useMutation({
    mutationFn: (input: IngestStart) => api.startLibraryIngest(libraryId, input),
    onSuccess: (_v, input) => {
      toast(
        input.mode === 'incremental'
          ? tr('已开始更新，进度见下方记录', 'Update started. Progress is in the history below.')
          : tr('已开始，进度见下方记录', 'Started. Progress is in the history below.'),
        'ok',
      );
      invalidateState();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('这个文献库已有任务在运行，请等它完成', 'A task is already running for this library. Wait for it to finish.'), 'error');
        invalidateState();
      } else {
        toast(`${tr('无法开始：', 'Couldn’t start: ')}${errorText(e)}`, 'error');
      }
    },
  });

  const discoverMutation = useMutation({
    mutationFn: async () => {
      const topic = keywords.trim() || includeKeywords.join(', ') || lib.statement?.trim() || lib.name;
      const created = await api.createLiteratureRun(libraryId, {
        topic,
        requested_count: resultCount,
        start_year: startYear,
        end_year: CURRENT_YEAR,
      });
      await api.startLiteratureRun(libraryId, created.id);
      return created;
    },
    onSuccess: (r) => {
      setOpenRunId(r.id);
      void queryClient.invalidateQueries({ queryKey: ['literature-runs', libraryId] });
      toast(tr('已开始检索，结果会列在下面', 'Searching. Results appear below.'), 'ok');
    },
    onError: (e) => toast(`${tr('无法开始检索：', 'Couldn’t start the search: ')}${errorText(e)}`, 'error'),
  });

  const saveAnchors = useMutation({
    mutationFn: (next: AnchorPaper[]) => api.updateLibrary(libraryId, { anchors: next }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['library', libraryId] }),
    onError: (e) => toast(`${tr('无法保存代表论文：', 'Couldn’t save the key papers: ')}${errorText(e)}`, 'error'),
  });

  const busy = running || ingestMutation.isPending;
  const noKeywords = includeKeywords.length === 0 && !keywords.trim();

  function search() {
    if (handling === 'pick') {
      discoverMutation.mutate();
      return;
    }
    ingestMutation.mutate({
      mode: 'search',
      knobs: { max_papers: maxPapers, relevance_threshold: threshold, compile_top_n: compileTopN, unlimited },
      query_terms: keywords.split(/[,，;；\n]/).map((x) => x.trim()).filter(Boolean),
      time_range: timeRange,
    });
  }

  const methods: { v: Method; label: string }[] = [
    { v: 'keywords', label: tr('关键词检索', 'Keywords') },
    { v: 'ids', label: tr('编号或 BibTeX', 'IDs or BibTeX') },
    { v: 'zotero', label: tr('从 Zotero 导入', 'From Zotero') },
    { v: 'citations', label: tr('顺着引用找', 'Follow citations') },
  ];

  const shown = showAll ? history : history.slice(0, 6);
  const queries = survey?.queries ?? [];
  const sourceLine = (survey?.sources ?? [])
    .map((s) => tr(`${s.source} ${s.fetched} 条`, `${s.source} ${s.fetched}`))
    .join(' · ');

  return (
    <div className="scroll fp-scroll" data-testid="find-papers">
      <div className="fp-col">
        {/* —— 保持更新 —— */}
        <section className="fp-section" aria-labelledby="fp-keep">
          <h2 className="fp-h" id="fp-keep">
            {tr('保持更新', 'Keep up to date')}
          </h2>
          {run && (
            <div className="fp-origin" data-testid="find-origin">
              <p>
                {tr('这个文献库是研究「', 'This library was built by the research on “')}
                <Link to={`/research/${run.id}`}>{run.name}</Link>
                {tr('」时检索建立的', '”')}
                {survey?.progress
                  ? tr(
                      `：检索到 ${survey.progress.fetched ?? survey.progress.found} 条，筛选了 ${survey.progress.screened} 篇，留下 ${survey.progress.kept} 篇。`,
                      `: ${survey.progress.fetched ?? survey.progress.found} results, ${survey.progress.screened} screened, ${survey.progress.kept} kept.`,
                    )
                  : tr('。', '.')}
              </p>
              <div className="fp-row-actions">
                {queries.length > 0 && (
                  <button type="button" className="btn btn-plain xs" onClick={() => setQueriesOpen((o) => !o)} aria-expanded={queriesOpen}>
                    {queriesOpen
                      ? tr('收起检索式', 'Hide the queries')
                      : tr(`查看 ${queries.length} 条检索式`, `Show the ${queries.length} queries`)}
                  </button>
                )}
                <Link className="btn btn-ghost xs" to={`/research/${run.id}?edit=scope`} data-testid="find-edit-scope">
                  {tr('调整范围并重新检索', 'Edit scope and search again')}
                  <Icon name="arrow" size={11} />
                </Link>
              </div>
              {queriesOpen && (
                <div className="fp-queries" data-testid="find-queries">
                  {sourceLine && <div className="fp-note">{sourceLine}</div>}
                  <ol>
                    {queries.slice(0, 40).map((q, i) => (
                      <li key={i}>
                        <span className="fp-q-src">{q.source}</span>
                        <span className="fp-q-text" title={q.query}>
                          {q.query}
                        </span>
                        <span className="fp-note">{tr(`${q.returned} 条`, `${q.returned}`)}</span>
                      </li>
                    ))}
                  </ol>
                  {queries.length > 40 && <div className="fp-note">{tr(`还有 ${queries.length - 40} 条没列出`, `${queries.length - 40} more not shown`)}</div>}
                </div>
              )}
            </div>
          )}
          <div className="fp-keep" data-testid="find-keep">
            <div className="fp-keep-text">
              {stateError ? (
                <span className="fp-note">{tr('暂时读不到更新状态，请确认本机引擎正在运行', 'Couldn’t read the update status. Check that the local engine is running.')}</span>
              ) : state?.watermark ? (
                <>
                  <span>{tr('每天从新发表的论文里挑出相关的加入', 'Each day, relevant new papers are added')}</span>
                  <span className="fp-note">
                    {[
                      state.last_run?.finished_at ? tr(`上次 ${fmtWhen(state.last_run.finished_at)}`, `last ${fmtWhen(state.last_run.finished_at)}`) : null,
                      state.next_sync_at ? tr(`下次 ${fmtWhen(state.next_sync_at)}`, `next ${fmtWhen(state.next_sync_at)}`) : null,
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                </>
              ) : (
                <>
                  <span>{tr('还没开始每日更新', 'Daily updates haven’t started')}</span>
                  <span className="fp-note">{tr('先用下面的办法找一批论文，之后每天自动补新的。', 'Find a first batch below; new papers are then added daily.')}</span>
                </>
              )}
            </div>
            <button
              type="button"
              className="btn btn-ghost sm"
              disabled={busy || !state?.watermark}
              title={!state?.watermark ? tr('找到第一批论文后可用', 'Available after the first batch') : undefined}
              onClick={() =>
                ingestMutation.mutate({
                  mode: 'incremental',
                  knobs: { max_papers: maxPapers, relevance_threshold: threshold, compile_top_n: compileTopN },
                })
              }
              data-testid="find-update-now"
            >
              <Icon name="refresh" size={13} />
              {tr('立即更新', 'Update now')}
            </button>
          </div>
        </section>

        {/* —— 找新论文 —— */}
        <section className="fp-section" aria-labelledby="fp-new">
          <h2 className="fp-h" id="fp-new">
            {tr('找新论文', 'Find new papers')}
          </h2>
          <div className="pl-quiet-tabs fp-methods" role="tablist">
            {methods.map((m) => (
              <button
                key={m.v}
                type="button"
                role="tab"
                aria-selected={method === m.v}
                className={'pl-qtab' + (method === m.v ? ' is-on' : '')}
                data-testid={`find-method-${m.v}`}
                onClick={() => setMethod(m.v)}
              >
                {m.label}
              </button>
            ))}
          </div>

          {method === 'keywords' && (
            <div className="fp-form">
              <label className="fp-field">
                <span className="fp-label">{tr('关键词', 'Keywords')}</span>
                <input
                  className="input"
                  value={keywords}
                  onChange={(e) => setKeywords(e.target.value)}
                  placeholder={
                    includeKeywords.length
                      ? tr(`留空就用：${includeKeywords.slice(0, 3).join(', ')}…`, `Leave empty to use: ${includeKeywords.slice(0, 3).join(', ')}…`)
                      : tr('例如 retrieval-augmented generation, joint training', 'e.g. retrieval-augmented generation, joint training')
                  }
                  data-testid="find-keywords"
                />
              </label>
              <div className="fp-field">
                <span className="fp-label">{tr('找到之后', 'Then')}</span>
                <div className="fp-radios" role="radiogroup">
                  <label className={'fp-radio' + (handling === 'pick' ? ' is-on' : '')}>
                    <input type="radio" name="fp-handling" checked={handling === 'pick'} onChange={() => setHandling('pick')} />
                    <span>
                      <b>{tr('先列出来，我来挑', 'List them, I pick')}</b>
                      <small>{tr('从多个来源检索、打分，勾选后才加入', 'Searches several sources and scores them; nothing is added until you choose')}</small>
                    </span>
                  </label>
                  <label className={'fp-radio' + (handling === 'auto' ? ' is-on' : '')}>
                    <input type="radio" name="fp-handling" checked={handling === 'auto'} onChange={() => setHandling('auto')} />
                    <span>
                      <b>{tr('相关的直接加入', 'Add relevant ones')}</b>
                      <small>{tr('检索 arXiv，够相关的直接加入并生成解读', 'Searches arXiv, adds the relevant ones and writes summaries')}</small>
                    </span>
                  </label>
                </div>
              </div>
              {handling === 'pick' ? (
                <div className="fp-inline">
                  <label className="fp-field fp-small">
                    <span className="fp-label">{tr('最多列出', 'Up to')}</span>
                    <input className="input" type="number" min={1} max={200} value={resultCount} onChange={(e) => setResultCount(Math.max(1, Math.min(200, Number(e.target.value) || 1)))} />
                  </label>
                  <label className="fp-field fp-small">
                    <span className="fp-label">{tr('从哪年起', 'From year')}</span>
                    <input className="input" type="number" min={1800} max={CURRENT_YEAR} value={startYear} onChange={(e) => setStartYear(Math.max(1800, Math.min(CURRENT_YEAR, Number(e.target.value) || CURRENT_YEAR)))} />
                  </label>
                </div>
              ) : (
                <>
                  <label className="fp-field fp-small">
                    <span className="fp-label">{tr('时间范围', 'Published within')}</span>
                    <select className="input" value={timeRange} onChange={(e) => setTimeRange(e.target.value as IngestTimeRange)}>
                      <option value="1w">{tr('近一周', 'Past week')}</option>
                      <option value="3m">{tr('近三个月', 'Past 3 months')}</option>
                      <option value="6m">{tr('近半年', 'Past 6 months')}</option>
                      <option value="1y">{tr('近一年', 'Past year')}</option>
                    </select>
                  </label>
                  <button type="button" className="btn btn-plain xs fp-more-btn" onClick={() => setMoreOpen((o) => !o)} aria-expanded={moreOpen}>
                    <Icon name="chevDown" size={11} style={{ transform: moreOpen ? 'rotate(180deg)' : 'none' }} />
                    {tr('数量和门槛', 'Limits')}
                  </button>
                  {moreOpen && (
                    <div className="fp-knobs">
                      <KnobRange
                        label={tr('相关度门槛', 'Relevance bar')}
                        hint={tr('低于这个分数的不加入', 'Papers below this aren’t added')}
                        value={threshold}
                        min={0.5}
                        max={0.95}
                        step={0.05}
                        format={(v) => v.toFixed(2)}
                        onChange={setThreshold}
                      />
                      <KnobRange label={tr('最多检索', 'Search at most')} value={maxPapers} min={20} max={500} step={10} onChange={setMaxPapers} disabled={unlimited} disabledText={tr('不限', 'No limit')} />
                      <KnobRange
                        label={tr('最多写解读', 'Summarize at most')}
                        hint={tr('按相关度取前几篇', 'The most relevant first')}
                        value={compileTopN}
                        min={0}
                        max={200}
                        step={5}
                        onChange={setCompileTopN}
                        disabled={unlimited}
                        disabledText={tr('不限', 'No limit')}
                      />
                      <label className="fp-switch">
                        <span>
                          {tr('不限篇数', 'No limits')}
                          <small>{tr('耗时和用量可能大幅增加', 'Time and usage may rise sharply')}</small>
                        </span>
                        <Switch checked={unlimited} onChange={setUnlimited} aria-label={tr('不限篇数', 'No limits')} />
                      </label>
                    </div>
                  )}
                </>
              )}
              <div className="fp-go">
                <button
                  type="button"
                  className="btn btn-primary sm"
                  disabled={discoverMutation.isPending || (handling === 'auto' && (busy || noKeywords))}
                  onClick={search}
                  data-testid="find-search"
                >
                  <Icon name={discoverMutation.isPending || ingestMutation.isPending ? 'refresh' : 'search'} size={13} style={discoverMutation.isPending || ingestMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
                  {tr('检索', 'Search')}
                </button>
                {handling === 'auto' && running && <span className="fp-note">{tr('已有任务在运行，等它完成再开始', 'A task is running; start after it finishes')}</span>}
                {handling === 'auto' && !running && noKeywords && (
                  <span className="fp-note">
                    {tr('先填关键词，或在', 'Enter keywords, or set them in ')}
                    <button type="button" className="link-btn" onClick={onOpenSettings}>
                      {tr('设置', 'Settings')}
                    </button>
                    {tr('里设好', '')}
                  </span>
                )}
              </div>
            </div>
          )}

          {(method === 'ids' || method === 'zotero') && (
            <div className="fp-form">
              <p className="fp-lead">
                {method === 'ids'
                  ? tr('粘贴 arXiv 编号、DOI、PMID 或 BibTeX，一次最多 50 篇。已在库里的会跳过。', 'Paste arXiv IDs, DOIs, PMIDs or BibTeX, up to 50 at a time. Papers already here are skipped.')
                  : tr('导入 Zotero 导出的 .bib 文件，可以带上附件压缩包里的 PDF。', 'Import a .bib file exported from Zotero, optionally with the PDFs from its attachment zip.')}
              </p>
              <div className="fp-go">
                <button
                  type="button"
                  className="btn btn-primary sm"
                  onClick={() => setAddMethod(method === 'ids' ? 'arxiv' : 'zotero')}
                  data-testid={method === 'ids' ? 'find-add-ids' : 'find-add-zotero'}
                >
                  <Icon name="plus" size={13} />
                  {method === 'ids' ? tr('粘贴编号或 BibTeX', 'Paste IDs or BibTeX') : tr('选择 .bib 文件', 'Choose a .bib file')}
                </button>
              </div>
            </div>
          )}

          {method === 'citations' && (
            <div className="fp-form">
              <p className="fp-lead">
                {tr(
                  '从几篇代表论文出发，沿它们引用的和引用它们的论文往外找，相关的加入并生成解读。',
                  'Starting from a few key papers, follow what they cite and what cites them; relevant ones are added and summarized.',
                )}
              </p>
              <div className="fp-field">
                <span className="fp-label">{tr('代表论文', 'Key papers')}</span>
                <AnchorEditor libraryId={libraryId} anchors={anchors} onChange={(next) => saveAnchors.mutate(next)} disabled={saveAnchors.isPending} />
              </div>
              <label className="fp-field fp-small">
                <span className="fp-label">{tr('往外找几层', 'How far')}</span>
                <select className="input" value={hops} onChange={(e) => setHops(Number(e.target.value) as 1 | 2 | 3)}>
                  <option value={1}>{tr('1 层', '1 step')}</option>
                  <option value={2}>{tr('2 层（耗时和用量约翻倍）', '2 steps (about twice the time)')}</option>
                  <option value={3}>{tr('3 层（耗时和用量更多）', '3 steps (much longer)')}</option>
                </select>
              </label>
              <div className="fp-go">
                <button
                  type="button"
                  className="btn btn-primary sm"
                  disabled={busy || anchors.length === 0}
                  onClick={() =>
                    ingestMutation.mutate({
                      mode: 'snowball',
                      knobs: { max_papers: maxPapers, relevance_threshold: threshold, snowball_depth: hops, compile_top_n: compileTopN, unlimited },
                    })
                  }
                  data-testid="find-snowball"
                >
                  <Icon name="layers" size={13} />
                  {tr('开始找', 'Start')}
                </button>
                {anchors.length === 0 && <span className="fp-note">{tr('先添加至少一篇代表论文', 'Add at least one key paper first')}</span>}
              </div>
            </div>
          )}
        </section>

        {/* —— 挑选式检索的结果 —— */}
        {openRunId && (
          <section className="fp-section fp-results" aria-label={tr('检索结果', 'Search results')} data-testid="find-results">
            <div className="fp-results-head">
              <h2 className="fp-h">{tr('检索结果', 'Search results')}</h2>
              <button type="button" className="btn btn-plain xs" onClick={() => setOpenRunId(null)}>
                {tr('收起', 'Close')}
              </button>
            </div>
            <LiteratureDiscoveryPanel libraryId={libraryId} runId={openRunId} embedded />
          </section>
        )}

        {/* —— 记录 —— */}
        <section className="fp-section" aria-labelledby="fp-history">
          <h2 className="fp-h" id="fp-history">
            {tr('记录', 'History')}
          </h2>
          {history.length === 0 ? (
            <p className="fp-note">{tr('还没有找过论文', 'No searches yet')}</p>
          ) : (
            <ul className="fp-history" data-testid="find-history">
              {shown.map((h) => (
                <li key={h.key}>
                  <button type="button" className="fp-history-row" onClick={h.onOpen} data-testid={h.testId}>
                    <span className="fp-history-main">
                      <span className="fp-history-title" title={h.title}>
                        {h.title}
                      </span>
                      <span className="fp-note">
                        {fmtWhen(h.at)} · {h.detail}
                      </span>
                    </span>
                    <span className="fp-history-status">
                      {h.status}
                      <Icon name="chevron" size={12} />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {history.length > 6 && (
            <button type="button" className="btn btn-plain xs" onClick={() => setShowAll((o) => !o)}>
              {showAll ? tr('收起', 'Show fewer') : tr(`显示全部 ${history.length} 条`, `Show all ${history.length}`)}
            </button>
          )}
        </section>
      </div>

      <AddPapersModal
        libraryId={libraryId}
        open={addMethod !== null}
        initialMethod={addMethod ?? 'arxiv'}
        onClose={() => setAddMethod(null)}
        onImported={onOpenPaper}
      />
    </div>
  );
}
