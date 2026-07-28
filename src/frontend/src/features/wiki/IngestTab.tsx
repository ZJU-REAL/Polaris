import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { FormField } from '../../components/ui/FormField';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { fmtTime } from '../../lib/format';
import {
  api,
  ApiError,
  type AnchorPaper,
  type IngestStart,
  type IngestState,
  type IngestTimeRange,
} from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   文献收集 Tab：
   - ingest 状态（水位线 / 论文计数 / 上次运行 / 进行中航程）
   - bootstrap 成本旋钮表单 → POST /ingest {mode:"bootstrap"}
   - 增量同步按钮 → {mode:"incremental"}
   ============================================================ */

export interface IngestTabProps {
  pid?: string;
  /** 独立库作用域：给定时抓取走 /libraries/{id}/ingest/run，状态走库端点 */
  libraryId?: string;
  state: IngestState | undefined;
  stateError: boolean;
  stateLoading: boolean;
  /** 切到本工作台「收录设置」（govern）tab；关键词提示据此跳转，无则退回导航。 */
  onGoGovern?: () => void;
  /** 只读（普通用户视角）：仅展示左侧状态列，隐藏增量同步 / 初始建库等触发入口。 */
  readOnly?: boolean;
}

// 模块级常量只存 zh/en 两份文案，渲染处再 tr（import 时求值不会随语言切换更新）
const COUNT_ROWS: { key: keyof NonNullable<IngestState['paper_counts']>; zh: string; en: string }[] = [
  { key: 'library', zh: '库内文献', en: 'In library' },
  { key: 'compiled', zh: '已编译', en: 'Compiled' },
  { key: 'pending_compile', zh: '待编译', en: 'To compile' },
  { key: 'included', zh: '人工精选', en: 'Hand-picked' },
  { key: 'candidate', zh: '未筛选', en: 'Unscreened' },
  { key: 'excluded', zh: '已删除', en: 'Deleted' },
];

function KnobRange({
  label,
  en,
  hint,
  value,
  min,
  max,
  step,
  format,
  onChange,
  disabled,
  disabledText,
}: {
  label: string;
  en: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format?: (v: number) => string;
  onChange: (v: number) => void;
  disabled?: boolean;
  disabledText?: string;
}) {
  return (
    <FormField label={label} en={en} hint={hint}>
      <div className="row gap12" style={disabled ? { opacity: 0.45 } : undefined}>
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(Number(e.target.value))}
          style={{ flex: 1 }}
        />
        <span
          className="mono"
          style={{ fontSize: 12.5, fontWeight: 650, minWidth: 44, textAlign: 'right', whiteSpace: 'nowrap' }}
        >
          {disabled && disabledText ? disabledText : format ? format(value) : value}
        </span>
      </div>
    </FormField>
  );
}

/** 锚点论文编辑器：只填 arXiv id，题目由系统补上。
 *
 * 从「收录设置」搬到这里——锚点是给「从锚点论文扩展」用的输入，放在收录设置里
 * 与检索/打分的配置混在一起，用户找不到它跟哪个动作有关。 */
function AnchorEditor({
  libraryId,
  anchors,
  onChange,
  disabled,
}: {
  libraryId?: string;
  anchors: AnchorPaper[];
  onChange: (next: AnchorPaper[]) => void;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);

  async function add() {
    const id = draft.trim();
    if (!id) return;
    if (anchors.some((a) => (a.arxiv_id || '').trim() === id)) {
      toast(tr('这篇已经在列表里了', 'Already in the list'), 'info');
      return;
    }
    setBusy(true);
    try {
      // 题目自动补：解析不出也照样加进去（id 是有效输入，题目只是给人看的确认）
      let title = '';
      try {
        title = (await api.resolvePaperByArxivId(id)).title;
      } catch {
        toast(tr('没解析出题目，已按 id 添加', 'Could not resolve the title — added by id'), 'info');
      }
      onChange([...anchors, { arxiv_id: id, title }]);
      setDraft('');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="col gap8">
      {anchors.length > 0 && (
        <div className="col gap6">
          {anchors.map((a, i) => (
            <div key={`${a.arxiv_id}-${i}`} className="row gap8" style={{ alignItems: 'center' }}>
              <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
                {a.arxiv_id}
              </span>
              <span style={{ fontSize: 12.5, flex: 1, minWidth: 0 }} className="ellipsis">
                {a.title || <span className="muted">{tr('（题目未解析）', '(title unresolved)')}</span>}
              </span>
              {!disabled && (
                <button
                  type="button"
                  className="btn btn-ghost sm"
                  onClick={() => onChange(anchors.filter((_, j) => j !== i))}
                >
                  <Icon name="x" size={11} />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {!disabled && (
        <div className="row gap8">
          <input
            className="input"
            style={{ flex: 1 }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void add(); } }}
            placeholder={tr('填 arXiv id，如 2005.11401', 'arXiv id, e.g. 2005.11401')}
          />
          <button type="button" className="btn btn-soft sm" disabled={busy || !draft.trim()} onClick={() => void add()}>
            {busy ? tr('解析中…', 'Resolving…') : tr('添加', 'Add')}
          </button>
        </div>
      )}
      {libraryId && anchors.length === 0 && (
        <div className="muted" style={{ fontSize: 11.5 }}>
          {tr('还没有锚点论文。填几篇这个方向的代表作，扩展会从它们的引用与参考文献往外走。',
              'No anchor papers yet. Add a few representative works — the expansion walks outward from their citations and references.')}
        </div>
      )}
    </div>
  );
}

export function IngestTab({ pid, libraryId, state, stateError, stateLoading, onGoGovern, readOnly = false }: IngestTabProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const scopeId = libraryId ?? pid ?? '';

  // 无 include 关键词时 arXiv 检索会退化成无差别抓取（空转烧钱），前端禁止启动**初始建库**。
  // 增量同步不受此限：它从每日论文池按方向语义粗排，关键词不参与筛选，没配也能跑。
  const { projects } = useProject();
  const project = libraryId ? undefined : projects.find((p) => p.id === pid);
  // 库作用域：真读该库 definition 的 include 关键词是否为空（拿不到库定义时不误报）。
  // 课题作用域（P9e：project.definition 退役）沿用旧代理，提示去文献库配置关键词。
  const { data: libDef } = useQuery({
    queryKey: ['library', libraryId],
    queryFn: () => api.getLibrary(libraryId as string),
    enabled: !!libraryId && !readOnly,
    retry: false,
  });
  // 锚点论文：从库定义读，改完就存回（这里是它唯一的编辑入口，收录设置里已移除）
  const anchors = libDef?.definition?.anchor_papers ?? [];
  const anchorCount = anchors.length;
  const saveAnchors = useMutation({
    mutationFn: (next: AnchorPaper[]) =>
      api.updateLibrary(libraryId as string, { anchors: next }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['library', libraryId] });
    },
    onError: (e) =>
      toast(`${tr('保存锚点失败', 'Failed to save anchors')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const noKeywords = libraryId
    ? !!libDef && (libDef.definition?.keywords?.include?.length ?? 0) === 0
    : !!project;

  // —— 成本旋钮（bootstrap） ——
  const [maxPapers, setMaxPapers] = useState(150);
  const [threshold, setThreshold] = useState(0.8);
  const [hops, setHops] = useState<'1' | '2' | '3'>('1');
  const [queryTerms, setQueryTerms] = useState('');
  const [timeRange, setTimeRange] = useState<IngestTimeRange>('6m');
  const [compileTopN, setCompileTopN] = useState(50);
  // 最大化模式：不限检索/编译篇数（仅 bootstrap 表单，增量同步不受影响）
  const [unlimited, setUnlimited] = useState(false);

  const running = !!state?.running_voyage_id;
  // 库可读 ≠ 库的任务可见：无权打开详情就不给跳转（见后端 can_open_running_voyage）
  const canOpenRunning = !!state?.can_open_running_voyage;

  const ingestMutation = useMutation({
    mutationFn: (input: IngestStart) =>
      libraryId ? api.startLibraryIngest(libraryId, input) : api.startIngest(scopeId, input),
    onSuccess: (v, input) => {
      toast(
        {
          search: tr('检索入库已开始，跳转任务详情…', 'Search started — opening task detail…'),
          snowball: tr('锚点扩展已开始，跳转任务详情…', 'Anchor expansion started — opening task detail…'),
          incremental: tr('同步已开始，跳转任务详情…', 'Sync started — opening task detail…'),
          bootstrap: tr('检索入库已开始，跳转任务详情…', 'Search started — opening task detail…'),
        }[input.mode],
        'ok',
      );
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409 && e.message === 'LIBRARY_BUDGET_EXHAUSTED') {
        toast(
          tr('这个文献库本月 AI 预算已用尽，同步已暂停；下月自动恢复，或请管理员调高预算。', 'This library has used up its monthly AI budget — syncing is paused until next month, or ask an admin to raise the budget.'),
          'error',
        );
      } else if (e instanceof ApiError && e.message === 'LIBRARY_NOT_ACTIVE') {
        toast(
          tr('文献库还未激活，管理员批准后才能开始抓取。', 'Library is not active yet — an admin must approve it before ingest can start.'),
          'error',
        );
      } else if (e instanceof ApiError && e.status === 409) {
        toast(
          tr('该文献库已有一个文献任务在运行，请等待其完成。', 'A literature task is already running for this library — wait for it to finish.'),
          'error',
        );
        void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
      } else {
        toast(`${tr('启动失败：', 'Failed to start: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  // 建库要关键词（拼 arXiv 检索式），增量同步不要——别把同步一起锁死
  const busy = running || ingestMutation.isPending;
  const bootstrapBusy = busy || noKeywords;

  /** 模式一：按查询词检索 arXiv。查询词留空时后端退回库里的「包括关键词」。 */
  function runSearch() {
    ingestMutation.mutate({
      mode: 'search',
      knobs: {
        max_papers: maxPapers,
        relevance_threshold: threshold,
        compile_top_n: compileTopN,
        unlimited,
      },
      query_terms: queryTerms.split(/[,，]/).map((x) => x.trim()).filter(Boolean),
      time_range: timeRange,
    });
  }

  /** 模式二：从锚点论文出发走引用/参考。不检索 arXiv。 */
  function runSnowball() {
    ingestMutation.mutate({
      mode: 'snowball',
      knobs: {
        max_papers: maxPapers,
        relevance_threshold: threshold,
        snowball_depth: Number(hops),
        compile_top_n: compileTopN,
        unlimited,
      },
    });
  }

  function runIncremental() {
    ingestMutation.mutate({
      mode: 'incremental',
      knobs: { max_papers: maxPapers, relevance_threshold: threshold, compile_top_n: compileTopN },
    });
  }

  const counts = state?.paper_counts;

  return (
    <div className="scroll" style={{ overflowY: 'auto', flex: 1, padding: '22px 24px 60px' }}>
      <div className="row gap20" style={{ alignItems: 'flex-start' }}>
        {/* —— 左：状态 —— */}
        <div className="col gap16" style={{ flex: 1, minWidth: 0 }}>
          {/* 进行中航程；无权打开详情时只显示状态、不给跳转（点了会 404） */}
          {running && state?.running_voyage_id && (
            <div
              className={canOpenRunning ? 'card card-pad hoverable' : 'card card-pad'}
              onClick={canOpenRunning ? () => navigate(`/voyages/${state.running_voyage_id}`) : undefined}
              style={{ borderColor: 'var(--accent-soft-2)', background: 'var(--accent-soft)' }}
            >
              <div className="row gap10">
                <span className="pill" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                  <span className="dot pulse" />
                  {tr('运行中', 'Running')}
                </span>
                <span style={{ fontSize: 13.5, fontWeight: 650 }}>{tr('文献任务进行中', 'Literature task in progress')}</span>
                {canOpenRunning && (
                  <Icon name="arrow" size={14} style={{ marginLeft: 'auto', color: 'var(--accent-text)' }} />
                )}
              </div>
              <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginTop: 8 }}>
                voyage {state.running_voyage_id.slice(0, 8)}…
              </div>
            </div>
          )}

          {/* 状态卡 */}
          <div className="card" style={{ overflow: 'hidden' }}>
            <div className="card-pad row" style={{ paddingBottom: 12, justifyContent: 'space-between' }}>
              <span className="section-h">
                <Icon name="clock" size={15} style={{ color: 'var(--accent)' }} />
                {tr('知识库状态', 'Ingest state')}
              </span>
            </div>
            {stateLoading ? (
              <div className="empty" style={{ padding: 24 }}>{tr('加载状态…', 'Loading state…')}</div>
            ) : stateError ? (
              <EmptyState
                compact
                icon="x"
                title={tr('无法加载状态', 'Failed to load state')}
                desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or API not ready.')}
              />
            ) : (
              <div style={{ padding: '0 22px 20px' }}>
                <div className="row gap16" style={{ marginBottom: 16 }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 4 }}>{tr('上次同步时间', 'Last sync')}</div>
                    <div className="mono" style={{ fontSize: 16, fontWeight: 700 }}>
                      {state?.watermark ? state.watermark.slice(0, 10) : '—'}
                    </div>
                    {!state?.watermark && (
                      <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 2 }}>
                        {tr('尚未运行过初始建库', 'Initial library build has not run yet')}
                      </div>
                    )}
                    <div style={{ fontSize: 11, color: 'var(--text-3)', margin: '10px 0 4px' }}>{tr('下次自动同步', 'Next auto sync')}</div>
                    <div className="mono" style={{ fontSize: 13, fontWeight: 650 }}>
                      {state?.next_sync_at
                        ? new Date(state.next_sync_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
                        : '—'}
                    </div>
                    {!state?.next_sync_at && (
                      <div style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 2 }}>
                        {tr(
                          '运行节奏为每日且完成初始建库后，才会自动同步',
                          'Auto sync starts only after the initial build is done and the schedule is daily',
                        )}
                      </div>
                    )}
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 4 }}>{tr('论文总数', 'Total papers')}</div>
                    <div className="mono" style={{ fontSize: 16, fontWeight: 700 }}>{counts?.total ?? '—'}</div>
                  </div>
                </div>

                <div className="row gap8 wrap">
                  {COUNT_ROWS.map((r) => (
                    <span key={String(r.key)} className="pill sm" style={{ background: 'var(--surface-2)' }}>
                      {tr(r.zh, r.en)}
                      <span className="mono" style={{ fontWeight: 700 }}>{counts?.[r.key] ?? 0}</span>
                    </span>
                  ))}
                </div>

                <div className="hr" style={{ margin: '16px 0 12px' }} />
                <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>{tr('上次运行', 'Last run')}</div>
                {state?.last_run ? (
                  <div
                    className={state.last_run.can_open ? 'row gap10 hoverable' : 'row gap10'}
                    onClick={
                      state.last_run.can_open
                        ? () => navigate(`/voyages/${state.last_run?.voyage_id ?? ''}`)
                        : undefined
                    }
                    style={{
                      border: '0.5px solid var(--border)',
                      borderRadius: 9,
                      padding: '9px 12px',
                      background: 'var(--surface-2)',
                    }}
                  >
                    <StatusPill status={state.last_run.status} sm />
                    <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                      {fmtTime(state.last_run.finished_at)}
                    </span>
                    <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginLeft: 'auto' }}>
                      {state.last_run.voyage_id.slice(0, 8)}…
                    </span>
                    {state.last_run.can_open && (
                      <Icon name="chevron" size={13} style={{ color: 'var(--text-4)' }} />
                    )}
                  </div>
                ) : (
                  <span className="muted" style={{ fontSize: 12.5 }}>{tr('暂无运行记录', 'No runs yet')}</span>
                )}
              </div>
            )}
          </div>

          {/* 增量同步 */}
          {!readOnly && (
          <div className="card card-pad">
            <div className="row gap10" style={{ marginBottom: 8 }}>
              <span className="section-h">
                <Icon name="refresh" size={15} style={{ color: 'var(--accent)' }} />
                {tr('增量同步', 'Incremental sync')}
              </span>
            </div>
            <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6, margin: '0 0 14px' }}>
              {tr(
                '从每日论文池里挑出与本库方向相关的新论文，打分后自动入库；本步骤不再检索 arXiv。已建库的文献库每天都会自动同步一次。',
                'Picks papers relevant to this library from the daily pool and adds the ones that score well; this step no longer queries arXiv. Built libraries sync once a day automatically.',
              )}
            </p>
            <button className="btn btn-ghost" disabled={busy || !state?.watermark} onClick={runIncremental}>
              <Icon name="refresh" size={14} />
              {tr('立即增量同步', 'Sync now')}
            </button>
            {!state?.watermark && !stateError && !stateLoading && (
              <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 8 }}>
                {tr('需先完成一次初始建库。', 'Run the initial library build first.')}
              </div>
            )}
          </div>
          )}
        </div>

        {/* —— 右：冷启动表单（仅可管理者） —— */}
        {!readOnly && (
        <div className="card card-pad" style={{ flex: 1.2, minWidth: 0 }}>
          <div className="row gap10" style={{ marginBottom: 6 }}>
            <span className="section-h">
              <Icon name="play" size={15} style={{ color: 'var(--accent)' }} />
              {tr('按查询词检索入库', 'Search and admit')}
            </span>
          </div>
          <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6, margin: '0 0 18px' }}>
            {tr(
              '按查询词走 arXiv 检索，打分筛选后精读编译。建库和日后扩充都用它；下面的选项控制本次开销。',
              'Searches arXiv by query terms, then scores, filters and compiles. Used both to build a library and to expand it later; the knobs below control this run’s cost.',
            )}
          </p>

          <FormField
            label={tr('查询词', 'Query terms')}
            en="query_terms"
            hint={tr(
              '留空就用收录设置里的「包括关键词」。多个词用逗号分隔。',
              'Leave empty to use the library’s include terms. Separate multiple terms with commas.',
            )}
          >
            <input
              className="input"
              value={queryTerms}
              onChange={(e) => setQueryTerms(e.target.value)}
              placeholder={tr('如 world model, video prediction', 'e.g. world model, video prediction')}
            />
          </FormField>
          <FormField label={tr('时间范围', 'Time range')} en="time_range">
            <Segmented<IngestTimeRange>
              options={[
                { v: '1w', label: tr('近一周', '1 week') },
                { v: '3m', label: tr('近三个月', '3 months') },
                { v: '6m', label: tr('近半年', '6 months') },
                { v: '1y', label: tr('近一年', '1 year') },
              ]}
              value={timeRange}
              onChange={setTimeRange}
            />
          </FormField>
          <KnobRange
            label={tr('相关度阈值', 'Relevance threshold')}
            en="relevance_threshold"
            hint={tr('LLM 相关性打分低于该阈值的论文将被过滤。', 'Papers scoring below this relevance threshold are filtered out.')}
            value={threshold}
            min={0}
            max={1}
            step={0.05}
            format={(v) => v.toFixed(2)}
            onChange={setThreshold}
          />
          <FormField label={tr('最大化（不限篇数）', 'Maximize (no paper cap)')} en="unlimited">
            <label className="row gap10" style={{ cursor: 'pointer', userSelect: 'none' }}>
              <input
                type="checkbox"
                checked={unlimited}
                onChange={(e) => setUnlimited(e.target.checked)}
                style={{ width: 15, height: 15, accentColor: 'var(--accent)' }}
              />
              <span style={{ fontSize: 12.5, fontWeight: 650 }}>
                {unlimited ? tr('已开启：不限篇数', 'On: no cap') : tr('未开启', 'Off')}
              </span>
            </label>
            {unlimited && (
              <div
                style={{
                  marginTop: 8,
                  padding: '8px 10px',
                  borderRadius: 8,
                  background: 'var(--warn-bg)',
                  color: 'var(--warn-tx)',
                  fontSize: 11.5,
                  fontWeight: 600,
                  lineHeight: 1.55,
                }}
              >
                {tr(
                  '注意：将抓取时间窗口内全部相关论文并全部精读编译，耗时与 LLM 费用可能显著增加，且不再受预算限制自动停止。',
                  'Note: every relevant paper in the window will be fetched and compiled. Time and LLM cost may increase substantially, and the run will not stop on a budget limit.',
                )}
              </div>
            )}
          </FormField>
          <KnobRange
            label={tr('最大检索篇数', 'Max papers')}
            en="max_papers"
            hint={tr(
              '本次最多检索并打分的论文数。',
              'How many papers this run retrieves and scores at most.',
            )}
            value={maxPapers}
            min={10}
            max={500}
            step={10}
            onChange={setMaxPapers}
            disabled={unlimited}
            disabledText={tr('无上限', 'No cap')}
          />
          <KnobRange
            label={tr('最大编译篇数', 'Max compiled papers')}
            en="compile_top_n"
            hint={tr(
              '打分排序后取前 N 篇下载 PDF 并精读编译，不超过最大检索篇数。',
              'Top N papers by score get their PDFs downloaded and compiled; capped by max papers.',
            )}
            value={compileTopN}
            min={5}
            max={200}
            step={5}
            onChange={setCompileTopN}
            disabled={unlimited}
            disabledText={tr('无上限', 'No cap')}
          />

          {noKeywords && (
            <div
              style={{
                margin: '0 0 12px',
                padding: '10px 12px',
                borderRadius: 8,
                background: 'var(--warn-bg)',
                color: 'var(--warn-tx)',
                fontSize: 12,
                fontWeight: 600,
                lineHeight: 1.55,
              }}
            >
              {tr(
                '这个文献库还没有配置检索关键词，无法启动文献追踪——先去收录设置配置关键词。',
                'This library has no search terms configured yet, so literature tracking cannot start — configure them in inclusion settings first.',
              )}
              <button
                className="btn btn-soft sm"
                style={{ marginTop: 8, display: 'flex' }}
                onClick={() => onGoGovern?.()}
              >
                <Icon name="sliders" size={13} />
                {tr('去收录设置配置关键词', 'Configure terms in inclusion settings')}
              </button>
            </div>
          )}
          <div className="row gap10" style={{ marginTop: 6 }}>
            <button className="btn btn-primary" disabled={bootstrapBusy} onClick={runSearch}>
              {ingestMutation.isPending ? (
                <>
                  <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                  {tr('启动中…', 'Starting…')}
                </>
              ) : (
                <>
                  <Icon name="play" size={14} />
                  {tr('开始检索入库', 'Run search')}
                </>
              )}
            </button>
            {running && (
              <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                {tr('已有任务运行中，暂不可启动', 'A task is already running — cannot start another')}
              </span>
            )}
          </div>

          {/* —— 模式二：从锚点论文扩展 —— */}
          <div className="hr" style={{ margin: '20px 0 16px' }} />
          <div className="row gap10" style={{ marginBottom: 6 }}>
            <span className="section-h">
              <Icon name="layers" size={15} style={{ color: 'var(--accent)' }} />
              {tr('从锚点论文扩展', 'Expand from anchor papers')}
            </span>
          </div>
          <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6, margin: '0 0 14px' }}>
            {tr(
              '从收录设置里的锚点论文出发，沿引用与参考文献往外走，打分筛选后精读编译。不检索 arXiv。',
              'Walks citations and references outward from the library’s anchor papers, then scores, filters and compiles. Does not query arXiv.',
            )}
          </p>
          <FormField
            label={tr('跳数', 'Hops')}
            en="snowball_depth"
            hint={tr(
              '沿引用关系往外走几层。每多一跳调用量成倍增长，3 跳很重，先试 1 跳。',
              'How many hops to walk. Each hop multiplies the API calls — 3 is heavy, start with 1.',
            )}
          >
            <Segmented<'1' | '2' | '3'>
              options={[
                { v: '1', label: tr('1 跳', '1 hop') },
                { v: '2', label: tr('2 跳', '2 hops') },
                { v: '3', label: tr('3 跳', '3 hops') },
              ]}
              value={hops}
              onChange={setHops}
            />
          </FormField>
          <FormField
            label={tr('锚点论文', 'Anchor papers')}
            en="anchor_papers"
            hint={tr(
              '只需填 arXiv id，题目由系统自动补上。扩展从这些论文的引用与参考文献往外走。',
              'Just the arXiv id — the title is filled in automatically. The expansion walks outward from these papers.',
            )}
          >
            <AnchorEditor
              libraryId={libraryId}
              anchors={anchors}
              onChange={(next) => saveAnchors.mutate(next)}
              disabled={saveAnchors.isPending}
            />
          </FormField>
          <div className="row gap10" style={{ marginTop: 6 }}>
            <button
              className="btn btn-ghost"
              disabled={busy || !anchorCount}
              onClick={runSnowball}
            >
              <Icon name="layers" size={14} />
              {tr('开始锚点扩展', 'Run anchor expansion')}
            </button>
          </div>
        </div>
        )}
      </div>
    </div>
  );
}
