import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { FormField } from '../../components/ui/FormField';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { fmtTime } from '../../lib/format';
import { api, ApiError, type IngestKnobs, type IngestMode, type IngestState } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   冷启动 / 增量同步 Tab：
   - ingest 状态（水位线 / 论文计数 / 上次运行 / 进行中航程）
   - bootstrap 成本旋钮表单 → POST /ingest {mode:"bootstrap"}
   - 增量同步按钮 → {mode:"incremental"}
   ============================================================ */

export interface IngestTabProps {
  pid: string;
  state: IngestState | undefined;
  stateError: boolean;
  stateLoading: boolean;
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

export function IngestTab({ pid, state, stateError, stateLoading }: IngestTabProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // 无 include 关键词时 arXiv 检索会退化成无差别抓取（空转烧钱），前端禁止启动
  const { projects } = useProject();
  const project = projects.find((p) => p.id === pid);
  const noKeywords = !!project && (project.definition?.keywords?.include ?? []).length === 0;

  // —— 成本旋钮（bootstrap） ——
  const [monthsBack, setMonthsBack] = useState(6);
  const [maxPapers, setMaxPapers] = useState(50);
  const [threshold, setThreshold] = useState(0.6);
  const [snowballDepth, setSnowballDepth] = useState<'0' | '1' | '2'>('1');
  const [compileTopN, setCompileTopN] = useState(20);
  // 最大化模式：不限检索/编译篇数（仅 bootstrap 表单，增量同步不受影响）
  const [unlimited, setUnlimited] = useState(false);

  const running = !!state?.running_voyage_id;

  const ingestMutation = useMutation({
    mutationFn: (input: { mode: IngestMode; knobs: IngestKnobs }) => api.startIngest(pid, input),
    onSuccess: (v, input) => {
      toast(
        input.mode === 'bootstrap'
          ? tr('初始建库已开始，跳转任务详情…', 'Initial library build started — opening task detail…')
          : tr('增量同步已开始，跳转任务详情…', 'Incremental sync started — opening task detail…'),
        'ok',
      );
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', pid] });
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409 && e.message === 'LIBRARY_BUDGET_EXHAUSTED') {
        toast(
          tr('这个文献库本月 AI 预算已用尽，同步已暂停；下月自动恢复，或请管理员调高预算。', 'This library has used up its monthly AI budget — syncing is paused until next month, or ask an admin to raise the budget.'),
          'error',
        );
      } else if (e instanceof ApiError && e.status === 409) {
        toast(
          tr('该课题已有一个文献任务在运行，请等待其完成。', 'A literature task is already running for this topic — wait for it to finish.'),
          'error',
        );
        void queryClient.invalidateQueries({ queryKey: ['ingest-state', pid] });
      } else {
        toast(`${tr('启动失败：', 'Failed to start: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  const busy = running || ingestMutation.isPending || noKeywords;

  function runBootstrap() {
    ingestMutation.mutate({
      mode: 'bootstrap',
      knobs: {
        months_back: monthsBack,
        max_papers: maxPapers,
        relevance_threshold: threshold,
        snowball_depth: Number(snowballDepth),
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
          {/* 进行中航程 */}
          {running && state?.running_voyage_id && (
            <div
              className="card card-pad hoverable"
              onClick={() => navigate(`/voyages/${state.running_voyage_id}`)}
              style={{ borderColor: 'var(--accent-soft-2)', background: 'var(--accent-soft)' }}
            >
              <div className="row gap10">
                <span className="pill" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                  <span className="dot pulse" />
                  {tr('运行中', 'Running')}
                </span>
                <span style={{ fontSize: 13.5, fontWeight: 650 }}>{tr('文献任务进行中', 'Literature task in progress')}</span>
                <Icon name="arrow" size={14} style={{ marginLeft: 'auto', color: 'var(--accent-text)' }} />
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 8, lineHeight: 1.55 }}>
                {tr(
                  '点击查看任务实时进度（SSE 流式）。运行期间无法再次启动 ingest。',
                  'Click to watch live progress (SSE stream). Ingest cannot be started again while it runs.',
                )}
              </div>
              <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginTop: 6 }}>
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
                    className="row gap10 hoverable"
                    onClick={() => navigate(`/voyages/${state.last_run?.voyage_id ?? ''}`)}
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
                    <Icon name="chevron" size={13} style={{ color: 'var(--text-4)' }} />
                  </div>
                ) : (
                  <span className="muted" style={{ fontSize: 12.5 }}>{tr('暂无运行记录', 'No runs yet')}</span>
                )}
              </div>
            )}
          </div>

          {/* 增量同步 */}
          <div className="card card-pad">
            <div className="row gap10" style={{ marginBottom: 8 }}>
              <span className="section-h">
                <Icon name="refresh" size={15} style={{ color: 'var(--accent)' }} />
                {tr('增量同步', 'Incremental sync')}
              </span>
            </div>
            <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6, margin: '0 0 14px' }}>
              {tr(
                '从上次同步时间之后抓取新论文并打分编译；已建库的项目每日也会由定时任务自动增量。',
                'Fetch, score, and compile papers published since the last sync; built libraries also sync daily on a schedule.',
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
        </div>

        {/* —— 右：冷启动表单 —— */}
        <div className="card card-pad" style={{ flex: 1.2, minWidth: 0 }}>
          <div className="row gap10" style={{ marginBottom: 6 }}>
            <span className="section-h">
              <Icon name="play" size={15} style={{ color: 'var(--accent)' }} />
              {tr('初始建库', 'Initial library build')}
            </span>
          </div>
          <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6, margin: '0 0 18px' }}>
            {tr(
              '初始建库一次性回填近 N 个月文献并做参考文献扩展，AI 打分筛选后精读编译建立知识库。以下选项控制本次开销。',
              'The initial build backfills the last N months of papers with reference expansion, then scores, filters, and compiles them into the library. These knobs control the cost of this run.',
            )}
          </p>

          <KnobRange
            label={tr('回填月数', 'Months back')}
            en="months_back"
            hint={tr('从今天往回抓取候选论文的时间窗口（3-24 个月）。', 'How far back from today to fetch candidate papers (3-24 months).')}
            value={monthsBack}
            min={3}
            max={24}
            step={1}
            onChange={setMonthsBack}
          />
          <FormField
            label={tr('最大化（不限篇数）', 'Maximize (no paper cap)')}
            en="unlimited"
            hint={tr(
              '开启后检索篇数与编译篇数不设上限，抓取时间窗口内的全部相关论文。',
              'When on, search and compile have no paper caps: every relevant paper in the window is processed.',
            )}
          >
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
              '本次检索与参考文献扩展最多加入知识库的论文数。',
              'Cap on papers added to the library from search plus reference expansion.',
            )}
            value={maxPapers}
            min={10}
            max={300}
            step={10}
            onChange={setMaxPapers}
            disabled={unlimited}
            disabledText={tr('无上限', 'No cap')}
          />
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
          <FormField
            label={tr('参考文献扩展层数', 'Reference expansion depth')}
            en="snowball_depth"
            hint={tr('沿引用关系扩展检索的层数；0 为不扩展。', 'How many hops to expand along citations; 0 disables expansion.')}
          >
            <Segmented<'0' | '1' | '2'>
              options={[
                { v: '0', label: tr('0 · 关', '0 · off') },
                { v: '1', label: tr('1 层', '1 hop') },
                { v: '2', label: tr('2 层', '2 hops') },
              ]}
              value={snowballDepth}
              onChange={setSnowballDepth}
            />
          </FormField>
          <KnobRange
            label={tr('最大编译篇数', 'Max compiled papers')}
            en="compile_top_n"
            hint={tr(
              '打分排序后取前 N 篇下载 PDF 并精读编译，不超过最大检索篇数。',
              'Top N papers by score get their PDFs downloaded and compiled; capped by max papers.',
            )}
            value={compileTopN}
            min={5}
            max={100}
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
                '这个课题还没有配置 include 关键词，无法启动文献追踪——先在课题设置里配置关键词。',
                'This topic has no include terms yet, so literature tracking cannot start — add them in topic settings first.',
              )}
              <button
                className="btn btn-soft sm"
                style={{ marginTop: 8, display: 'flex' }}
                onClick={() => navigate(`/projects/${pid}`)}
              >
                <Icon name="sliders" size={13} />
                {tr('去课题设置配置关键词', 'Configure terms in topic settings')}
              </button>
            </div>
          )}
          <div className="row gap10" style={{ marginTop: 6 }}>
            <button className="btn btn-primary" disabled={busy} onClick={runBootstrap}>
              {ingestMutation.isPending ? (
                <>
                  <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                  {tr('启动中…', 'Starting…')}
                </>
              ) : (
                <>
                  <Icon name="play" size={14} />
                  {tr('运行初始建库', 'Run initial library build')}
                </>
              )}
            </button>
            {running && (
              <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                {tr('已有任务运行中，暂不可启动', 'A task is already running — cannot start another')}
              </span>
            )}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 12, lineHeight: 1.6 }}>
            {tr(
              '以 AI 任务呈现进度，支持断点续跑；同一项目同时只允许一个 ingest 任务。',
              'Progress shows as an AI task and can resume from checkpoints; one ingest task per direction at a time.',
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
