import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { FormField } from '../../components/ui/FormField';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { fmtTime } from '../../lib/format';
import { api, ApiError, type IngestKnobs, type IngestMode, type IngestState } from '../../lib/api';

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

const COUNT_ROWS: { key: keyof NonNullable<IngestState['paper_counts']>; zh: string }[] = [
  { key: 'library', zh: '库内文献' },
  { key: 'compiled', zh: '已编译' },
  { key: 'pending_compile', zh: '待编译' },
  { key: 'included', zh: '人工精选' },
  { key: 'candidate', zh: '未筛选' },
  { key: 'excluded', zh: '已删除' },
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
}) {
  return (
    <FormField label={label} en={en} hint={hint}>
      <div className="row gap12">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          style={{ flex: 1 }}
        />
        <span className="mono" style={{ fontSize: 12.5, fontWeight: 650, width: 44, textAlign: 'right' }}>
          {format ? format(value) : value}
        </span>
      </div>
    </FormField>
  );
}

export function IngestTab({ pid, state, stateError, stateLoading }: IngestTabProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // —— 成本旋钮（bootstrap） ——
  const [monthsBack, setMonthsBack] = useState(6);
  const [maxPapers, setMaxPapers] = useState(50);
  const [threshold, setThreshold] = useState(0.6);
  const [snowballDepth, setSnowballDepth] = useState<'0' | '1' | '2'>('1');
  const [compileTopN, setCompileTopN] = useState(20);

  const running = !!state?.running_voyage_id;

  const ingestMutation = useMutation({
    mutationFn: (input: { mode: IngestMode; knobs: IngestKnobs }) => api.startIngest(pid, input),
    onSuccess: (v, input) => {
      toast(input.mode === 'bootstrap' ? '初始建库已开始，跳转任务详情…' : '增量同步已开始，跳转任务详情…', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', pid] });
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast('该项目已有一个文献任务在运行，请等待其完成。', 'error');
        void queryClient.invalidateQueries({ queryKey: ['ingest-state', pid] });
      } else {
        toast(`启动失败：${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  const busy = running || ingestMutation.isPending;

  function runBootstrap() {
    ingestMutation.mutate({
      mode: 'bootstrap',
      knobs: {
        months_back: monthsBack,
        max_papers: maxPapers,
        relevance_threshold: threshold,
        snowball_depth: Number(snowballDepth),
        compile_top_n: compileTopN,
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
                  运行中
                </span>
                <span style={{ fontSize: 13.5, fontWeight: 650 }}>文献任务进行中</span>
                <Icon name="arrow" size={14} style={{ marginLeft: 'auto', color: 'var(--accent-text)' }} />
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 8, lineHeight: 1.55 }}>
                点击查看任务实时进度（SSE 流式）。运行期间无法再次启动 ingest。
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
                知识库状态 <span className="en-label" style={{ fontSize: 11 }}>Ingest state</span>
              </span>
            </div>
            {stateLoading ? (
              <div className="empty" style={{ padding: 24 }}>加载状态…</div>
            ) : stateError ? (
              <EmptyState compact icon="x" title="无法加载状态" desc="后端不可用或接口尚未就绪。" />
            ) : (
              <div style={{ padding: '0 22px 20px' }}>
                <div className="row gap16" style={{ marginBottom: 16 }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 4 }}>上次同步时间 last sync</div>
                    <div className="mono" style={{ fontSize: 16, fontWeight: 700 }}>
                      {state?.watermark ? state.watermark.slice(0, 10) : '—'}
                    </div>
                    {!state?.watermark && (
                      <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 2 }}>尚未运行过初始建库</div>
                    )}
                    <div style={{ fontSize: 11, color: 'var(--text-3)', margin: '10px 0 4px' }}>下次自动同步 next sync</div>
                    <div className="mono" style={{ fontSize: 13, fontWeight: 650 }}>
                      {state?.next_sync_at
                        ? new Date(state.next_sync_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
                        : '—'}
                    </div>
                    {!state?.next_sync_at && (
                      <div style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 2 }}>
                        运行节奏为每日且完成初始建库后，才会自动同步
                      </div>
                    )}
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 4 }}>论文总数 total</div>
                    <div className="mono" style={{ fontSize: 16, fontWeight: 700 }}>{counts?.total ?? '—'}</div>
                  </div>
                </div>

                <div className="row gap8 wrap">
                  {COUNT_ROWS.map((r) => (
                    <span key={String(r.key)} className="pill sm" style={{ background: 'var(--surface-2)' }}>
                      {r.zh}
                      <span className="mono" style={{ fontWeight: 700 }}>{counts?.[r.key] ?? 0}</span>
                    </span>
                  ))}
                </div>

                <div className="hr" style={{ margin: '16px 0 12px' }} />
                <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>上次运行 last run</div>
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
                  <span className="muted" style={{ fontSize: 12.5 }}>暂无运行记录</span>
                )}
              </div>
            )}
          </div>

          {/* 增量同步 */}
          <div className="card card-pad">
            <div className="row gap10" style={{ marginBottom: 8 }}>
              <span className="section-h">
                <Icon name="refresh" size={15} style={{ color: 'var(--accent)' }} />
                增量同步 <span className="en-label" style={{ fontSize: 11 }}>Incremental</span>
              </span>
            </div>
            <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6, margin: '0 0 14px' }}>
              从上次同步时间之后抓取新论文并打分编译；已建库的项目每日也会由定时任务自动增量。
            </p>
            <button className="btn btn-ghost" disabled={busy || !state?.watermark} onClick={runIncremental}>
              <Icon name="refresh" size={14} />
              立即增量同步
            </button>
            {!state?.watermark && !stateError && !stateLoading && (
              <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 8 }}>需先完成一次初始建库 bootstrap。</div>
            )}
          </div>
        </div>

        {/* —— 右：冷启动表单 —— */}
        <div className="card card-pad" style={{ flex: 1.2, minWidth: 0 }}>
          <div className="row gap10" style={{ marginBottom: 6 }}>
            <span className="section-h">
              <Icon name="play" size={15} style={{ color: 'var(--accent)' }} />
              初始建库 <span className="en-label" style={{ fontSize: 11 }}>Bootstrap</span>
            </span>
          </div>
          <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6, margin: '0 0 18px' }}>
            初始建库一次性回填近 N 个月文献并做参考文献扩展，
            AI 打分筛选后精读编译建立知识库。以下选项控制本次开销。
          </p>

          <KnobRange
            label="回填月数"
            en="months_back"
            hint="从今天往回抓取候选论文的时间窗口（3-24 个月）。"
            value={monthsBack}
            min={3}
            max={24}
            step={1}
            onChange={setMonthsBack}
          />
          <KnobRange
            label="最大检索篇数"
            en="max_papers"
            hint="本次检索与参考文献扩展的收录规模上限，也是任务成本上限。"
            value={maxPapers}
            min={10}
            max={300}
            step={10}
            onChange={setMaxPapers}
          />
          <KnobRange
            label="相关度阈值"
            en="relevance_threshold"
            hint="LLM 相关性打分低于该阈值的论文将被过滤。"
            value={threshold}
            min={0}
            max={1}
            step={0.05}
            format={(v) => v.toFixed(2)}
            onChange={setThreshold}
          />
          <FormField label="参考文献扩展层数" en="snowball_depth" hint="沿引用关系扩展检索的层数；0 为不扩展。">
            <Segmented<'0' | '1' | '2'>
              options={[
                { v: '0', label: '0 · 关' },
                { v: '1', label: '1 层' },
                { v: '2', label: '2 层' },
              ]}
              value={snowballDepth}
              onChange={setSnowballDepth}
            />
          </FormField>
          <KnobRange
            label="最大编译篇数"
            en="compile_top_n"
            hint="打分排序后取前 N 篇下载 PDF 并精读编译，不超过最大检索篇数。"
            value={compileTopN}
            min={5}
            max={100}
            step={5}
            onChange={setCompileTopN}
          />

          <div className="row gap10" style={{ marginTop: 6 }}>
            <button className="btn btn-primary" disabled={busy} onClick={runBootstrap}>
              {ingestMutation.isPending ? (
                <>
                  <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                  启动中…
                </>
              ) : (
                <>
                  <Icon name="play" size={14} />
                  运行初始建库 bootstrap
                </>
              )}
            </button>
            {running && (
              <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>已有任务运行中，暂不可启动</span>
            )}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 12, lineHeight: 1.6 }}>
            以 AI 任务呈现进度，支持断点续跑；同一项目同时只允许一个 ingest 任务。
          </div>
        </div>
      </div>
    </div>
  );
}
