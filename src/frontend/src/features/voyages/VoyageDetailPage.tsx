import { useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { VoyageActions } from '../../components/ui/VoyageActions';
import { StatusPill } from '../../components/ui/StatusPill';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { toast } from '../../components/ui/Toast';
import { useShell } from '../../app/AppShell';
import { topicPath, useProject } from '../../app/project';
import { fmtDuration, fmtTime, fmtTokens } from '../../lib/format';
import { tr } from '../../lib/i18n';
import {
  api,
  ApiError,
  isLabScopedTask,
  VOYAGE_TERMINAL,
  type VoyageStepRead,
} from '../../lib/api';
import { KindBadge } from './VoyagesPage';
import { ActivityBar } from './shared/ActivityBar';
import { PlanEventCard, StepCard } from './shared/StepCard';
import {
  asObj,
  buildTimelineEntries,
  byListOrder,
  num,
  stepMarker,
  stepTokenCount,
} from './shared/stepUtils';
import { TaskTerminal, type TerminalExtraEntry } from './shared/terminal';
import { useVoyageChannel } from './shared/useVoyageChannel';
import { openAskOf, useVoyageMessages } from './shared/useVoyageMessages';
import { messageNode } from './shared/AskBlock';
import { ConsoleComposer } from './shared/ConsoleComposer';

/* ============================================================
   /voyages/:id — 任务详情：循环感知的活动状态 + 步骤时间线 + SSE 实时。
   体现背后 agent 的规划 → 执行 → 校验 → 按结果调整计划循环：
   - 顶部显示当前活动的一句话（而非线性四段进度条）与执行方式徽标；
   - 步骤卡展示验收标准、判定理由、来源（第几次调整新增）、尝试记录；
   - 时间线按 plan_history 插入计划调整分隔条目，解释为什么多出新步骤。
   活动状态订阅 /voyages/{id}/events，事件与 TanStack Query 缓存合并。
   共享组件（StepCard / ActivityBar / TaskTerminal / useVoyageChannel）
   在 ./shared/ 下，与实验运行台共用。
   ============================================================ */

// —— 论文分享 PPT（kind=presentation）：完成后下载产物 ——

function PresentationDownload({ voyageId, goal }: { voyageId: string; goal: string }) {
  const downloadMutation = useMutation({
    mutationFn: () => api.downloadPresentation(voyageId),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${goal.replace(/[/\\:*?"<>|]/g, ' ').slice(0, 60)}.pptx`;
      a.click();
      URL.revokeObjectURL(url);
    },
    onError: (e) =>
      toast(
        e instanceof ApiError && e.message === 'FILE_NOT_READY'
          ? tr('文件还没生成好，稍后再试', 'File not ready yet — try again shortly')
          : `${tr('下载失败：', 'Download failed: ')}${e instanceof Error ? e.message : String(e)}`,
        'error',
      ),
  });
  return (
    <button
      className="btn btn-primary"
      disabled={downloadMutation.isPending}
      onClick={() => downloadMutation.mutate()}
    >
      <Icon name="download" size={13} />
      {downloadMutation.isPending ? tr('下载中…', 'Downloading…') : tr('下载 PPT', 'Download PPT')}
    </button>
  );
}

// —— 文献任务（wiki_bootstrap / wiki_ingest）：整体结果汇总卡 ——

/** 只用来决定要不要渲染建库摘要卡；不含每日新论文（它没有这种 observation）。
    「这个任务归实验室还是归课题」用 lib/api 的 isLabScopedTask，别拿这个判。 */
const WIKI_RUN_KINDS = new Set(['wiki_bootstrap', 'wiki_ingest']);

/** 文献任务的整体结果卡：从各步 observation 汇总本次新增/编译数量。 */
function WikiRunSummary({ steps }: { steps: VoyageStepRead[] }) {
  const obsOf = (action: string) =>
    asObj(steps.find((s) => s.action === action && s.status === 'passed')?.observation);
  const search = obsOf('wiki.search_candidates');
  const snowball = obsOf('wiki.snowball');
  const score = obsOf('wiki.score_relevance');
  const compile = obsOf('wiki.compile');
  const link = obsOf('wiki.link_concepts');
  if (!search && !compile) return null;

  const stats: { label: string; value: number }[] = [];
  if (search || snowball) stats.push({ label: tr('新收录论文', 'New papers'), value: num(search?.inserted) + num(snowball?.inserted) });
  if (score) stats.push({ label: tr('通过筛选', 'Passed screening'), value: num(score.succeeded) - num(score.excluded) });
  if (compile) stats.push({ label: tr('已编译', 'Compiled'), value: num(compile.succeeded) });
  if (link) stats.push({ label: tr('新增概念', 'New concepts'), value: num(link.concepts_promoted) });
  if (stats.length === 0) return null;

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="row" style={{ marginBottom: 10 }}>
        <span className="section-h">
          <Icon name="book" size={15} style={{ color: 'var(--accent)' }} />
          {tr('本次同步结果', 'Sync summary')}
        </span>
      </div>
      <div className="row" style={{ gap: 28, flexWrap: 'wrap' }}>
        {stats.map((s) => (
          <div key={s.label}>
            <div className="mono" style={{ fontSize: 22, fontWeight: 680, lineHeight: 1.2 }}>{s.value}</div>
            <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{s.label}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// —— 页面 ——

export function VoyageDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openGates } = useShell();
  const { currentProjectId } = useProject();
  const [showObsolete, setShowObsolete] = useState(false);

  const { data: voyage, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['voyage', id, showObsolete],
    queryFn: () => api.getVoyage(id, { includeObsolete: showObsolete }),
    retry: false,
    enabled: !!id,
  });

  const resumeMutation = useMutation({
    mutationFn: () => api.resumeVoyage(id),
    onSuccess: () => {
      toast(tr('已重新入队，从断点续跑', 'Re-queued — resuming from where it stopped'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    },
    onError: (err) => toast(`${tr('重试失败：', 'Retry failed: ')}${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  const active = !!voyage && !VOYAGE_TERMINAL.has(voyage.status);

  // 对话流（用户建议 / AI 提问）+ 终端 SSE 实时通道
  const { messages, handleExtraEvent } = useVoyageMessages(id);
  const { terminal, live, clearTerminal, historyLoading, historyError } = useVoyageChannel(
    id,
    active,
    { onExtraEvent: handleExtraEvent },
  );
  const openAsk = openAskOf(voyage, messages);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const extraEntries = useMemo<TerminalExtraEntry[]>(() => {
    const out: TerminalExtraEntry[] = [];
    for (const m of messages) {
      const node = messageNode(m);
      if (node) out.push({ id: m.id, at: m.created_at, node });
    }
    return out;
  }, [messages]);
  const focusComposer = () => {
    composerRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    composerRef.current?.focus();
  };

  if (isLoading) {
    return (
      <div className="page fadeup">
        <div className="empty" style={{ padding: 80 }}>{tr('加载中…', 'Loading…')}</div>
      </div>
    );
  }
  if (isError || !voyage) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="page fadeup">
        <div className="card card-pad" style={{ textAlign: 'center', padding: 60 }}>
          <div style={{ fontSize: 15, fontWeight: 650, marginBottom: 8 }}>
            {notFound ? tr('任务不存在', 'Task not found') : tr('无法加载任务详情', 'Failed to load task detail')}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 18 }}>
            {error instanceof Error ? error.message : tr('后端不可用，请稍后重试', 'Backend unavailable — try again later')}
          </div>
          <div className="row gap8" style={{ justifyContent: 'center' }}>
            <button className="btn btn-soft" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
            <button className="btn btn-ghost" onClick={() => navigate(topicPath(currentProjectId, 'voyages'))}>{tr('返回列表', 'Back to list')}</button>
          </div>
        </div>
      </div>
    );
  }

  const steps = [...(voyage.steps ?? [])].sort(byListOrder);
  const planEvents = voyage.plan_history ?? [];
  const entries = buildTimelineEntries(steps, planEvents);
  const totalTokens = steps.reduce((acc, s) => acc + (stepTokenCount(s.tokens) ?? 0), 0);
  const planAdjusted = (voyage.plan_iteration ?? 0) > 0;

  return (
    <div className="page fadeup" style={{ maxWidth: 920 }}>
      {/* 页头 */}
      <div className="row" style={{ alignItems: 'flex-start', marginBottom: 20 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-eyebrow row gap8">
            {/* 返回按任务层级分流：文献库任务（建库/增量更新）与每日新论文归实验室
                工作台，其余归课题工作台。判据与面包屑同一份（isLabScopedTask）——
                原来这里单用 WIKI_RUN_KINDS，漏掉每日新论文，把它送去了课题工作台；
                而它 project_id 为空，跳课题只会落到首页。 */}
            <span
              className="row gap6"
              style={{ cursor: 'pointer' }}
              onClick={() =>
                navigate(
                  isLabScopedTask(voyage)
                    ? '/lab?tab=tasks'
                    : topicPath(voyage.project_id, 'voyages'),
                )
              }
            >
              {isLabScopedTask(voyage)
                ? tr('← 实验室任务', '← Lab tasks')
                : tr('← 课题任务', '← Topic tasks')}
            </span>
            <span className="mono" style={{ textTransform: 'none', color: 'var(--text-4)' }}>{voyage.id.slice(0, 8)}</span>
            {live && (
              <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                <span className="dot pulse" />
                LIVE
              </span>
            )}
          </div>
          <h1 className="h-title" style={{ fontSize: 21 }}>{voyage.goal}</h1>
          <div className="row gap8" style={{ marginTop: 10, flexWrap: 'wrap' }}>
            <KindBadge kind={voyage.kind} />
            <StatusPill status={voyage.status} sm />
            {planAdjusted && (
              <span
                className="pill sm"
                style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}
                title={tr('执行过程中计划被调整过（自动重试后的调整 / 按执行结果追加的轮次）', 'The plan was adjusted during execution (after auto-retries / extra rounds based on results)')}
              >
                {tr('计划调整', 'Plan adjusted')} ×{voyage.plan_iteration}
              </span>
            )}
            <span className="mono muted" style={{ fontSize: 11 }}>
              {tr('创建', 'Created')} {fmtTime(voyage.created_at)} · {tr('耗时', 'took')} {fmtDuration(voyage.created_at, active ? null : voyage.updated_at)}
            </span>
            {totalTokens > 0 && (
              <span className="mono muted" style={{ fontSize: 11 }}>· {fmtTokens(totalTokens)} tokens</span>
            )}
          </div>
        </div>
        {/* 取消 / 删除；续跑在下方 ActivityBar 里（紧挨报错说明，那里更好理解） */}
        <VoyageActions voyage={voyage} showResume={false} onDone={() => navigate('/voyages')} />
        {voyage.kind === 'presentation' && voyage.status === 'done' && (
          <PresentationDownload voyageId={voyage.id} goal={voyage.goal} />
        )}
      </div>

      {/* 当前活动（循环感知：执行中的任务可能反复经过 执行→校验→调整计划） */}
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <ActivityBar
          voyage={voyage}
          steps={steps}
          onOpenGates={() => openGates(null)}
          onResume={() => resumeMutation.mutate()}
          resuming={resumeMutation.isPending}
          onReply={focusComposer}
        />
      </div>

      {/* 文献任务：本次同步结果汇总 */}
      {WIKI_RUN_KINDS.has(voyage.kind) && <WikiRunSummary steps={steps} />}

      {/* 本次任务使用的技能（启动时快照，中途改技能不影响） */}
      {(voyage.skills ?? []).length > 0 && (
        <div className="card card-pad" style={{ marginBottom: 20 }}>
          <div className="row gap8" style={{ flexWrap: 'wrap', alignItems: 'center' }}>
            <span style={{ fontSize: 12, color: 'var(--text-3)', flexShrink: 0 }}>{tr('本次任务使用的技能：', 'Skills used in this task:')}</span>
            {voyage.skills!.map((s) => (
              <span
                key={`${s.slug}-${s.target}`}
                className="pill sm"
                style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}
                title={`${s.slug} v${s.version} · ${s.target}`}
              >
                {s.name} v{s.version}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 步骤时间线（任务板：清单序渲染，计划调整插入分隔条目，作废步骤可选显示） */}
      <div className="row" style={{ marginBottom: 12 }}>
        <span className="section-h">
          <Icon name="compass" size={15} style={{ color: 'var(--accent)' }} />
          {tr('步骤时间线', 'Steps')}
        </span>
        {planAdjusted && (
          <label
            className="row gap6"
            style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-3)', cursor: 'pointer', userSelect: 'none' }}
          >
            <input
              type="checkbox"
              checked={showObsolete}
              onChange={(e) => setShowObsolete(e.target.checked)}
            />
            {tr('显示已作废步骤', 'Show obsolete steps')}
          </label>
        )}
      </div>
      {entries.length === 0 ? (
        <div className="card card-pad empty" style={{ padding: 40 }}>
          {tr('正在规划步骤…', 'Planning steps…')}
        </div>
      ) : (
        <Timeline>
          {entries.map((entry, i) => {
            const last = i === entries.length - 1;
            if (entry.kind === 'plan') {
              return (
                <TimelineItem
                  key={`plan-${entry.event.iteration}`}
                  marker={<Icon name="refresh" size={12} />}
                  markerBg="var(--accent-soft)"
                  markerColor="var(--accent-text)"
                  last={last}
                >
                  <PlanEventCard event={entry.event} />
                </TimelineItem>
              );
            }
            const m = stepMarker(entry.step);
            return (
              <TimelineItem key={entry.step.id} marker={entry.index} markerBg={m.bg} markerColor={m.color} last={last}>
                <StepCard step={entry.step} planEvents={planEvents} />
              </TimelineItem>
            );
          })}
        </Timeline>
      )}

      {/* 运行日志终端：结构化日志 + 大模型流式输出 + 对话混排，常驻显示 */}
      <TaskTerminal
        state={terminal}
        live={live}
        onClear={clearTerminal}
        extraEntries={extraEntries}
        historyLoading={historyLoading}
        historyError={historyError}
        footer={
          <ConsoleComposer
            voyageId={id}
            openAsk={openAsk}
            disabled={!!voyage && VOYAGE_TERMINAL.has(voyage.status)}
            inputRef={composerRef}
          />
        }
      />
    </div>
  );
}
