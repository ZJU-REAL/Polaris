import { useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { Switch } from '../../components/ui/Switch';
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
  type ResolvedPaperBatchItem,
} from '../../lib/api';
import { recognizePaperRef, refInput, refKey, refLabel } from '../../lib/paper-ref';
import { tr } from '../../lib/i18n';
import { splitPaperInput } from './paperInput';
import { PanelHint, PanelSection as Section } from './shared';

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
}

// 模块级常量只存 zh/en 两份文案，渲染处再 tr（import 时求值不会随语言切换更新）
const COUNT_ROWS: { key: keyof NonNullable<IngestState['paper_counts']>; zh: string; en: string }[] = [
  { key: 'library', zh: '在库', en: 'In library' },
  { key: 'compiled', zh: '已解读', en: 'Summarized' },
  { key: 'pending_compile', zh: '待解读', en: 'Awaiting summary' },
  { key: 'included', zh: '手动精选', en: 'Hand-picked' },
  { key: 'candidate', zh: '未筛选', en: 'Unscreened' },
  { key: 'excluded', zh: '已删除', en: 'Deleted' },
];

/** 设置行：左边标签（+ 一行说明），右边控件。 */
function Row({
  label,
  hint,
  title,
  warn,
  children,
}: {
  label: string;
  hint?: string;
  title?: string;
  warn?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="settings-row" style={{ flexWrap: 'wrap', rowGap: 8 }}>
      <div className="settings-row-text" style={{ minWidth: 180 }} title={title}>
        <div style={{ fontSize: 13 }}>{label}</div>
        {hint && (
          <div style={{ fontSize: 12, color: warn ? 'var(--warn-tx)' : 'var(--text-3)', marginTop: 2, lineHeight: 1.5 }}>
            {hint}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

/** 等宽数值（表格数字）。 */
function Value({ children }: { children: ReactNode }) {
  return (
    <span className="mono" style={{ fontSize: 13, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>
      {children}
    </span>
  );
}

/** 8px 状态点（配一个词用，不用彩色胶囊）。 */
function StatusDot({ tone, pulse }: { tone: 'ok' | 'warn' | 'idle'; pulse?: boolean }) {
  const color = tone === 'ok' ? 'var(--ok)' : tone === 'warn' ? 'var(--warn)' : 'var(--text-4)';
  return (
    <span
      className={pulse ? 'pulse' : undefined}
      style={{ width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0, display: 'inline-block' }}
    />
  );
}

function KnobRange({
  label,
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
    <Row label={label} hint={hint}>
      <div className="row gap12" style={{ width: 280, maxWidth: '100%', ...(disabled ? { opacity: 0.45 } : {}) }}>
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={disabled}
          aria-label={label}
          onChange={(e) => onChange(Number(e.target.value))}
          style={{ flex: 1, minWidth: 0 }}
        />
        <span
          className="mono"
          style={{ fontSize: 12, fontWeight: 600, minWidth: 44, textAlign: 'right', whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}
        >
          {disabled && disabledText ? disabledText : format ? format(value) : value}
        </span>
      </div>
    </Row>
  );
}

const MAX_ANCHOR_PAPERS = 50;

/** 草稿 → 认得出的标识（去重）与认不出的原文。标识不限 arXiv（#821）：DOI、PMID 同样可以当锚点。 */
function parseAnchorRefs(raw: string): { refs: string[]; unknown: string[] } {
  const refs: string[] = [];
  const unknown: string[] = [];
  const seen = new Set<string>();
  for (const piece of splitPaperInput(raw)) {
    const ref = recognizePaperRef(piece);
    if (!ref) {
      unknown.push(piece);
      continue;
    }
    const key = refKey(refInput(ref));
    if (!seen.has(key)) {
      seen.add(key);
      refs.push(piece);
    }
  }
  return { refs, unknown };
}

/** 解析结果 → 锚点：按类型落到 arxiv_id / doi / pmid 之一。 */
function anchorFromResolved(raw: string, item: ResolvedPaperBatchItem | undefined): AnchorPaper {
  const ref = recognizePaperRef(raw);
  const base: AnchorPaper = { title: item?.title || '' };
  if (!ref) return base;
  if (ref.kind === 'arxiv') return { ...base, arxiv_id: item?.arxiv_id || ref.value };
  if (ref.kind === 'doi') return { ...base, doi: item?.doi || ref.value };
  return { ...base, pmid: item?.pmid || ref.value };
}

/** 锚点论文编辑器：批量填写 arXiv 编号 / DOI / PMID，题目由系统一次性补上。
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
    const existing = new Set(anchors.map(refKey));
    const { refs, unknown } = parseAnchorRefs(draft);
    if (unknown.length > 0) {
      toast(
        tr(
          `无法识别：${unknown.join('、')}。请填写 arXiv 编号、DOI 或 PMID`,
          `Couldn’t recognise ${unknown.join(', ')}. Use arXiv IDs, DOIs or PMIDs.`,
        ),
        'error',
      );
      return;
    }
    const ids = refs.filter((raw) => {
      const ref = recognizePaperRef(raw);
      return ref ? !existing.has(refKey(refInput(ref))) : false;
    });
    if (ids.length === 0) {
      toast(tr('已在列表中', 'Already in the list'), 'info');
      return;
    }
    if (anchors.length + ids.length > MAX_ANCHOR_PAPERS) {
      toast(
        tr(
          `锚点论文最多 ${MAX_ANCHOR_PAPERS} 篇，已有 ${anchors.length} 篇`,
          `You can add up to ${MAX_ANCHOR_PAPERS} anchor papers. You have ${anchors.length}.`,
        ),
        'error',
      );
      return;
    }
    setBusy(true);
    try {
      const resolved = await api.resolvePaperRefs(ids);
      const additions = ids.map((raw, index) => anchorFromResolved(raw, resolved.items[index]));
      const failed = resolved.items.filter((item) => item.error).length;
      onChange([...anchors, ...additions]);
      if (failed > 0) {
        toast(
          tr(
            `已添加，其中 ${failed} 篇未找到题目`,
            `Added. Couldn’t find ${failed === 1 ? 'the title for 1 paper' : `titles for ${failed} papers`}.`,
          ),
          'info',
        );
      }
      setDraft('');
    } catch {
      onChange([...anchors, ...ids.map((raw) => anchorFromResolved(raw, undefined))]);
      setDraft('');
      toast(
        tr('已按编号添加，暂时无法查询题目', 'Added by ID. Titles couldn’t be looked up.'),
        'info',
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="col gap8">
      {anchors.length > 0 && (
        <div className="col gap6">
          {anchors.map((a, i) => (
            <div key={`${refKey(a)}-${i}`} className="row gap8" style={{ alignItems: 'center' }}>
              <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
                {refLabel(a)}
              </span>
              <span style={{ fontSize: 12.5, flex: 1, minWidth: 0 }} className="ellipsis">
                {a.title || <span className="muted">{tr('（无题目）', '(no title)')}</span>}
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
        <div className="col gap8">
          <textarea
            className="textarea mono"
            style={{ width: '100%', minHeight: 72, resize: 'vertical', fontSize: 12 }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                void add();
              }
            }}
            placeholder={tr(
              '例如 2005.11401, 10.1038/s41586-020-2649-2, PMID:31452104',
              'e.g. 2005.11401, 10.1038/s41586-020-2649-2, PMID:31452104',
            )}
          />
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button type="button" className="btn btn-soft sm" disabled={busy || !draft.trim()} onClick={() => void add()}>
              {busy
                ? tr('解析中…', 'Resolving…')
                : parseAnchorRefs(draft).refs.length > 1
                  ? tr(`添加 ${parseAnchorRefs(draft).refs.length} 篇`, `Add ${parseAnchorRefs(draft).refs.length} papers`)
                  : tr('添加', 'Add')}
            </button>
          </div>
        </div>
      )}
      {libraryId && anchors.length === 0 && (
        <div className="muted" style={{ fontSize: 11.5 }}>
          {tr('还没有锚点论文。添加几篇这个方向的代表作。', 'No anchor papers yet. Add a few key papers in this area.')}
        </div>
      )}
    </div>
  );
}

export function IngestTab({ pid, libraryId, state, stateError, stateLoading, onGoGovern }: IngestTabProps) {
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
    enabled: !!libraryId,
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
      toast(`${tr('无法保存锚点论文', 'Couldn’t save anchor papers')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
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
          search: tr('已开始检索', 'Search started'),
          snowball: tr('已开始扩展', 'Expansion started'),
          incremental: tr('已开始同步', 'Sync started'),
          bootstrap: tr('已开始检索', 'Search started'),
        }[input.mode],
        'ok',
      );
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409 && e.message === 'LIBRARY_BUDGET_EXHAUSTED') {
        toast(
          tr('本月用量预算已用完，下月自动恢复', 'This month’s usage budget is used up. It resets next month.'),
          'error',
        );
      } else if (e instanceof ApiError && e.status === 409) {
        toast(
          tr('这个文献库已有任务在运行，请等它完成', 'A task is already running for this library. Wait for it to finish.'),
          'error',
        );
        void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
      } else {
        toast(`${tr('无法启动：', 'Couldn’t start: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
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
  const fmtShort = (iso: string) =>
    new Date(iso).toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  return (
    <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
      <div className="col" style={{ gap: 20, maxWidth: 760, margin: '0 auto', padding: '4px 24px 48px' }}>
        {/* —— 概况 —— */}
        <Section
          first
          label={tr('概况', 'Overview')}
          action={
            running && state?.running_voyage_id ? (
              // 无权打开详情时只显示状态、不给跳转（点了会 404）
              canOpenRunning ? (
                <button className="btn btn-ghost sm" onClick={() => navigate(`/voyages/${state.running_voyage_id}`)}>
                  <StatusDot tone="ok" pulse />
                  {tr('任务运行中', 'Task running')}
                  <Icon name="arrow" size={12} />
                </button>
              ) : (
                <span className="row gap6" style={{ fontSize: 12, color: 'var(--text-2)' }}>
                  <StatusDot tone="ok" pulse />
                  {tr('任务运行中', 'Task running')}
                </span>
              )
            ) : undefined
          }
        >
          {stateLoading ? (
            <div className="skel" style={{ height: 120 }} />
          ) : stateError ? (
            <EmptyState
              compact
              icon="x"
              title={tr('无法加载概况', 'Couldn’t load the overview')}
              desc={tr('请确认本机引擎正在运行。', 'Check that the local engine is running.')}
            />
          ) : (
            <div className="settings-list">
              <Row label={tr('论文', 'Papers')} hint={COUNT_ROWS.map((r) => `${tr(r.zh, r.en)} ${counts?.[r.key] ?? 0}`).join(' · ')}>
                <Value>{counts?.total ?? '—'}</Value>
              </Row>
              <Row label={tr('上次同步时间', 'Last synced')} hint={state?.watermark ? undefined : tr('尚未初始建库', 'Not built yet')}>
                <Value>{state?.watermark ? state.watermark.slice(0, 10) : '—'}</Value>
              </Row>
              <Row
                label={tr('预计下次同步', 'Next sync (est.)')}
                hint={state?.next_sync_at ? undefined : tr('初始建库后，按每日节奏自动同步', 'Runs daily after the first build')}
                title={tr(
                  '跟随每日论文抓取，有新论文才同步，时间为估计',
                  'Follows the daily paper fetch and runs only when there are new papers, so the time is an estimate',
                )}
              >
                <Value>{state?.next_sync_at ? fmtShort(state.next_sync_at) : '—'}</Value>
              </Row>
              <Row label={tr('上次运行', 'Last run')}>
                {state?.last_run ? (
                  <button
                    type="button"
                    className="btn btn-ghost sm"
                    disabled={!state.last_run.can_open}
                    style={{ gap: 8 }}
                    onClick={() => navigate(`/voyages/${state.last_run?.voyage_id ?? ''}`)}
                  >
                    <StatusPill status={state.last_run.status} sm />
                    <span className="mono" style={{ fontSize: 12, color: 'var(--text-3)' }}>
                      {fmtTime(state.last_run.finished_at)}
                    </span>
                    {state.last_run.can_open && <Icon name="chevron" size={12} style={{ color: 'var(--text-3)' }} />}
                  </button>
                ) : (
                  <span className="muted" style={{ fontSize: 13 }}>{tr('还没有运行过', 'No runs yet')}</span>
                )}
              </Row>
            </div>
          )}
        </Section>

        {/* —— 同步新论文 —— */}
        <Section
          label={tr('同步新论文', 'Sync new papers')}
          action={
            <button
              className="btn btn-ghost sm"
              disabled={busy || !state?.watermark}
              title={!state?.watermark ? tr('完成初始建库后可用', 'Available after the first build') : undefined}
              onClick={runIncremental}
            >
              <Icon name="refresh" size={13} />
              {tr('立即同步', 'Sync now')}
            </button>
          }
        >
          <PanelHint>
            {tr(
              '从每天的新论文中挑出相关的加入本库，每天自动运行一次。',
              'Adds relevant papers from each day’s new arrivals. Runs once a day on its own.',
            )}
          </PanelHint>
        </Section>

        {/* —— 检索论文 —— */}
        <Section label={tr('检索论文', 'Search for papers')}>
          <PanelHint>
            {tr(
              '按关键词检索 arXiv，把相关论文加入本库并生成解读。',
              'Searches arXiv by keyword, adds relevant papers and generates their summaries.',
            )}
          </PanelHint>
          <div className="settings-list">
            <Row
              label={tr('关键词', 'Keywords')}
              hint={tr('留空则用收录设置中的关键词', 'Leave empty to use the library’s keywords')}
            >
              <input
                className="input"
                style={{ width: 280, maxWidth: '100%' }}
                value={queryTerms}
                onChange={(e) => setQueryTerms(e.target.value)}
                placeholder={tr('例如 world model, video prediction', 'e.g. world model, video prediction')}
              />
            </Row>
            <Row label={tr('时间范围', 'Time range')}>
              <Segmented<IngestTimeRange>
                options={[
                  { v: '1w', label: tr('近一周', 'Past week') },
                  { v: '3m', label: tr('近三个月', 'Past 3 months') },
                  { v: '6m', label: tr('近半年', 'Past 6 months') },
                  { v: '1y', label: tr('近一年', 'Past year') },
                ]}
                value={timeRange}
                onChange={setTimeRange}
              />
            </Row>
            <KnobRange
              label={tr('相关度阈值', 'Relevance threshold')}
              hint={tr('低于此分数的论文不加入', 'Papers scoring below this are skipped')}
              value={threshold}
              min={0}
              max={1}
              step={0.05}
              format={(v) => v.toFixed(2)}
              onChange={setThreshold}
            />
            <Row
              label={tr('不限篇数', 'No paper limit')}
              hint={
                unlimited
                  ? tr('耗时和用量可能大幅增加，且不受预算限制', 'Time and usage may rise sharply, with no budget limit')
                  : undefined
              }
              warn={unlimited}
            >
              <Switch checked={unlimited} onChange={setUnlimited} aria-label={tr('不限篇数', 'No paper limit')} />
            </Row>
            <KnobRange
              label={tr('最多检索篇数', 'Max papers to search')}
              value={maxPapers}
              min={10}
              max={500}
              step={10}
              onChange={setMaxPapers}
              disabled={unlimited}
              disabledText={tr('不限', 'No limit')}
            />
            <KnobRange
              label={tr('最多生成解读篇数', 'Max summaries')}
              hint={tr('按相关度取前几篇', 'Taken from the most relevant papers')}
              value={compileTopN}
              min={5}
              max={200}
              step={5}
              onChange={setCompileTopN}
              disabled={unlimited}
              disabledText={tr('不限', 'No limit')}
            />
          </div>

          <div className="row gap10 wrap" style={{ marginTop: 4 }}>
            <button className="btn btn-primary" disabled={bootstrapBusy} onClick={runSearch}>
              {ingestMutation.isPending ? (
                <>
                  <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                  {tr('启动中…', 'Starting…')}
                </>
              ) : (
                <>
                  <Icon name="play" size={14} />
                  {tr('开始检索', 'Start search')}
                </>
              )}
            </button>
            {noKeywords ? (
              <span className="row gap8" style={{ fontSize: 12, color: 'var(--warn-tx)' }}>
                {tr('还没有设置关键词，无法检索。', 'Set keywords before searching.')}
                <button className="btn btn-ghost sm" onClick={() => onGoGovern?.()}>
                  {tr('设置关键词', 'Set keywords')}
                </button>
              </span>
            ) : running ? (
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                {tr('已有任务在运行', 'A task is already running')}
              </span>
            ) : null}
          </div>
        </Section>

        {/* —— 从锚点论文扩展 —— */}
        <Section
          label={tr('从锚点论文扩展', 'Expand from anchor papers')}
          action={
            <button className="btn btn-ghost sm" disabled={busy || !anchorCount} onClick={runSnowball}>
              <Icon name="layers" size={13} />
              {tr('开始扩展', 'Start expansion')}
            </button>
          }
        >
          <PanelHint>
            {tr(
              '沿锚点论文的引用和参考文献找到相关论文，加入本库并生成解读。',
              'Follows the citations and references of your anchor papers to find related work.',
            )}
          </PanelHint>
          <div className="settings-list">
            <Row
              label={tr('扩展层数', 'Depth')}
              hint={tr('每多一层，耗时和用量成倍增加', 'Each level multiplies time and usage')}
            >
              <Segmented<'1' | '2' | '3'>
                options={[
                  { v: '1', label: tr('1 层', '1 level') },
                  { v: '2', label: tr('2 层', '2 levels') },
                  { v: '3', label: tr('3 层', '3 levels') },
                ]}
                value={hops}
                onChange={setHops}
              />
            </Row>
            <div className="col" style={{ gap: 8, padding: '12px 0 4px' }}>
              <div style={{ fontSize: 13 }}>{tr('锚点论文', 'Anchor papers')}</div>
              <AnchorEditor
                libraryId={libraryId}
                anchors={anchors}
                onChange={(next) => saveAnchors.mutate(next)}
                disabled={saveAnchors.isPending}
              />
            </div>
          </div>
        </Section>
      </div>
    </div>
  );
}
