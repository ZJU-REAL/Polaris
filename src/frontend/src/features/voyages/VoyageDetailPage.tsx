import { useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { VoyageActions } from '../../components/ui/VoyageActions';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { toast } from '../../components/ui/Toast';
import { useShell } from '../../app/AppShell';
import { topicPath } from '../../app/project';
import { errorText } from '../../lib/errors';
import { fmtTokens } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { api, ApiError, VOYAGE_TERMINAL, type VoyageStepRead } from '../../lib/api';
import { useDirections } from '../deepresearch/directions';
import { useNow } from '../deepresearch/hooks';
import { InlineQuestion } from '../deepresearch/InlineQuestion';
import { elapsedText } from '../deepresearch/phase';
import { DiscoveryPanel } from './discovery';
import { ParkedRunNotice } from './shared/ParkedRunNotice';
import { PlanEventCard, StepCard } from './shared/StepCard';
import { asObj, buildTimelineEntries, byListOrder, num, stepMarker, stepTokenCount } from './shared/stepUtils';
import { taskStatusLine, taskTitle, taskTone } from './shared/taskText';
import { TaskTerminal, type TerminalExtraEntry } from './shared/terminal';
import { useVoyageChannel } from './shared/useVoyageChannel';
import { openAskOf, useVoyageMessages } from './shared/useVoyageMessages';
import { messageNode } from './shared/AskBlock';
import { ConsoleComposer } from './shared/ConsoleComposer';
import '../deepresearch/deepresearch.css';
import './task.css';

/* ============================================================
   /voyages/:id — 一个任务的页面。读起来是一张进度单，不是控制台：

   · 顶上一句话：现在在做什么 / 在等你什么 / 结果如何，以及用了多久；
   · 需要你的时候（AI 提问、审批、出错停下），要做的事就在这句话下面，一个主按钮；
   · 步骤：每步一行名字 + 状态 + 一句结果；时间、用量、验收标准、尝试记录、原始数据在「详情」里；
   · 日志默认收起，点「显示日志」才出来（给想看过程、想给 AI 留一句话的人）。

   任务和步骤的名字由界面按类型 / 动作名给出（shared/taskText.ts），不显示引擎写给日志的标题。
   状态订阅 /voyages/{id}/events，事件与 TanStack Query 缓存合并。
   StepCard / TaskTerminal / useVoyageChannel 在 ./shared/ 下，与实验页共用。
   ============================================================ */

// —— 汇报幻灯片（kind=presentation）：完成后下载 ——

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
          ? tr('文件还没生成，请稍后再试', 'The file isn’t ready yet. Try again shortly.')
          : `${tr('无法下载：', 'Couldn’t download: ')}${errorText(e)}`,
        'error',
      ),
  });
  return (
    <button className="btn btn-primary sm" disabled={downloadMutation.isPending} onClick={() => downloadMutation.mutate()}>
      <Icon name="download" size={13} />
      {downloadMutation.isPending ? tr('下载中…', 'Downloading…') : tr('下载幻灯片', 'Download slides')}
    </button>
  );
}

/** 检索文献的任务：这次的结果，一句话（各步结果里的数字加起来）。没有可说的返回 null。 */
function surveyResultText(steps: VoyageStepRead[]): string | null {
  const obsOf = (action: string) => asObj(steps.find((s) => s.action === action && s.status === 'passed')?.observation);
  const search = obsOf('wiki.search_candidates');
  const snowball = obsOf('wiki.snowball');
  const score = obsOf('wiki.score_relevance');
  const compile = obsOf('wiki.compile');
  if (!search && !compile) return null;
  const parts: string[] = [];
  if (search || snowball) {
    const added = num(search?.inserted) + num(snowball?.inserted);
    parts.push(tr(`新收录 ${added} 篇`, `${added} new ${added === 1 ? 'paper' : 'papers'}`));
  }
  if (score) {
    const relevant = num(score.succeeded) - num(score.excluded);
    parts.push(tr(`${relevant} 篇相关`, `${relevant} relevant`));
  }
  if (compile) parts.push(tr(`写了 ${num(compile.succeeded)} 篇解读`, `${num(compile.succeeded)} summarised`));
  return parts.length > 0 ? parts.join(' · ') : null;
}

export function VoyageDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openGates } = useShell();
  const { byProject, byLibrary } = useDirections();
  const [showObsolete, setShowObsolete] = useState(false);
  const [showLog, setShowLog] = useState(false);

  const { data: voyage, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['voyage', id, showObsolete],
    queryFn: () => api.getVoyage(id, { includeObsolete: showObsolete }),
    retry: false,
    enabled: !!id,
  });

  const resumeMutation = useMutation({
    mutationFn: () => api.resumeVoyage(id),
    onSuccess: () => {
      toast(tr('已从出错的步骤继续', 'Resumed from the failed step'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    },
    onError: (err) => toast(`${tr('无法重试：', 'Couldn’t retry: ')}${errorText(err)}`, 'error'),
  });

  const active = !!voyage && !VOYAGE_TERMINAL.has(voyage.status);

  // 对话流（你留的话 / AI 的提问）+ 日志的实时通道。日志收起时通道照样开着：
  // 任务和步骤的状态也是从这里来的。
  const { messages, handleExtraEvent } = useVoyageMessages(id);
  const { terminal, live, clearTerminal, historyLoading, historyError } = useVoyageChannel(id, active, {
    onExtraEvent: handleExtraEvent,
  });
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
  const now = useNow(30_000, active);

  if (isLoading) {
    return (
      <div className="page fadeup task-page">
        <LoadingState rows={5} />
      </div>
    );
  }
  if (isError || !voyage) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="page fadeup task-page">
        <ErrorState
          title={
            notFound
              ? tr('找不到这个任务，可能已被删除', 'This task no longer exists.')
              : tr('无法加载任务，请确认本机引擎正在运行', 'Couldn’t load this task. Check that the local engine is running.')
          }
          onRetry={notFound ? undefined : () => void refetch()}
          action={
            <Link className="btn btn-ghost sm" to="/activity">
              {tr('查看全部任务', 'View all tasks')}
            </Link>
          }
        />
      </div>
    );
  }

  // 这个任务属于哪个课题：研究用的文献库的检索任务挂在文献库上，也算那个课题的
  const topic = byLibrary(voyage.library_id) ?? byProject(voyage.project_id);
  const partOfRun = !!topic?.run && (voyage.kind === 'discovery' || voyage.kind.startsWith('wiki_'));
  const back = topic
    ? { to: partOfRun ? topic.href : topicPath(topic.projectId, 'tasks'), label: topic.name }
    : { to: '/activity', label: tr('任务与待办', 'Activity') };

  const steps = [...(voyage.steps ?? [])].sort(byListOrder);
  const liveSteps = steps.filter((s) => s.status !== 'obsolete');
  const planEvents = voyage.plan_history ?? [];
  // 计划是怎么调整的（哪一步之后加了哪些步骤）默认不画：对多数人这是过程噪音，勾上才显示
  const entries = buildTimelineEntries(steps, showObsolete ? planEvents : []);
  const totalTokens = steps.reduce((acc, s) => acc + (stepTokenCount(s.tokens) ?? 0), 0);
  const planAdjusted = (voyage.plan_iteration ?? 0) > 0;
  const passed = liveSteps.filter((s) => s.status === 'passed').length;
  const result = voyage.kind.startsWith('wiki_') ? surveyResultText(steps) : null;
  const meta = [
    elapsedText(voyage.created_at, active ? null : voyage.updated_at, now),
    liveSteps.length > 0 && active ? tr(`已完成 ${passed}/${liveSteps.length} 步`, `${passed} of ${liveSteps.length} steps done`) : '',
    totalTokens > 0 ? `${fmtTokens(totalTokens)} tokens` : '',
  ]
    .filter(Boolean)
    .join(' · ');
  // 出错停下、且没有排自动重试：重试是这一页的主动作
  const stuck = voyage.status === 'paused_error';

  return (
    <div className="page fadeup task-page">
      <Link className="task-back" to={back.to}>
        ← {back.label}
      </Link>
      <div className="task-head">
        <h1 className="task-title" title={voyage.goal}>
          {taskTitle(voyage)}
        </h1>
        <div className="task-actions">
          {voyage.kind === 'presentation' && voyage.status === 'done' && (
            <PresentationDownload voyageId={voyage.id} goal={voyage.goal} />
          )}
          {/* 取消 / 删除；重试在下面的说明旁边（紧挨着出了什么事，那里更好理解） */}
          <VoyageActions voyage={voyage} showResume={false} onDone={() => navigate(back.to)} />
        </div>
      </div>

      {/* —— 一句话：在做什么 / 等你什么 / 结果如何 —— */}
      <div className="task-status" data-task-status={voyage.status}>
        {/* 只要一个状态点：那句话本身已经说了是完成、在等还是出错，不再重复一个状态词 */}
        <span className={`task-dot is-${taskTone(voyage.status)}`} aria-hidden="true" />
        <span className="task-status-text">{taskStatusLine(voyage, liveSteps)}</span>
        {meta && <span className="task-status-meta">{meta}</span>}
      </div>
      {result && <div className="task-result">{result}</div>}

      {/* —— 需要你的时候：要做的事就在这里 —— */}
      {openAsk && active && (
        <InlineQuestion
          voyageId={id}
          ask={openAsk}
          onAnswered={() => {
            void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
            void queryClient.invalidateQueries({ queryKey: ['voyage-messages', id] });
            void queryClient.invalidateQueries({ queryKey: ['voyages'] });
          }}
        />
      )}
      {voyage.status === 'paused_gate' && (
        <div className="dr-attn">
          <div className="dr-attn-text">{tr('这一步需要你审批', 'This step needs your approval')}</div>
          <div className="dr-attn-sub">{tr('批准后任务从这里继续。', 'The task continues from here once you approve.')}</div>
          <div className="dr-attn-actions">
            <button type="button" className="btn btn-primary sm" onClick={() => openGates(null)}>
              {tr('查看审批', 'Review')}
            </button>
          </div>
        </div>
      )}
      {stuck && (
        <div className="task-parked">
          <ParkedRunNotice
            error={voyage.last_error}
            retryAt={voyage.retry_at}
            retryAttempt={voyage.retry_attempt}
            onRetry={() => resumeMutation.mutate()}
            retrying={resumeMutation.isPending}
          />
        </div>
      )}

      {/* 寻找假设：假设树和报告是这类任务的结果，放在步骤前面 */}
      {voyage.kind === 'discovery' && <DiscoveryPanel voyage={voyage} />}

      {/* —— 步骤 —— */}
      <div className="task-sec-head">
        <h2 className="task-sec-title">{tr('步骤', 'Steps')}</h2>
        {planAdjusted && (
          <label className="task-sec-aside">
            <input type="checkbox" checked={showObsolete} onChange={(e) => setShowObsolete(e.target.checked)} />
            {tr('显示计划的调整', 'Show plan changes')}
          </label>
        )}
      </div>
      {entries.length === 0 ? (
        <div className="task-empty">{tr('正在安排步骤…', 'Working out the steps…')}</div>
      ) : (
        <Timeline>
          {entries.map((entry, i) => {
            const last = i === entries.length - 1;
            if (entry.kind === 'plan') {
              return (
                <TimelineItem
                  key={`plan-${entry.event.iteration}`}
                  marker={<Icon name="refresh" size={12} />}
                  markerBg="var(--surface-2)"
                  markerColor="var(--text-3)"
                  last={last}
                >
                  <PlanEventCard event={entry.event} />
                </TimelineItem>
              );
            }
            const m = stepMarker(entry.step);
            return (
              <TimelineItem key={entry.step.id} marker={entry.index} markerBg={m.bg} markerColor={m.color} last={last}>
                <StepCard step={entry.step} planEvents={planEvents} showProvenance={showObsolete} />
              </TimelineItem>
            );
          })}
        </Timeline>
      )}

      {/* —— 日志：默认收起 —— */}
      <div className="task-log">
        <button
          type="button"
          className="btn btn-ghost sm"
          data-log-toggle
          aria-expanded={showLog}
          onClick={() => setShowLog((s) => !s)}
        >
          <Icon name="chevDown" size={12} style={{ transform: showLog ? 'rotate(180deg)' : 'none' }} />
          {showLog ? tr('隐藏日志', 'Hide log') : tr('显示日志', 'Show log')}
        </button>
        {!showLog && active && (
          <span className="task-log-hint">
            {tr('日志里可以给 AI 留一句话，它会在下一步之前看到。', 'In the log you can leave a note for the AI. It reads it before the next step.')}
          </span>
        )}
        {showLog && (
          <TaskTerminal
            state={terminal}
            live={live}
            onClear={clearTerminal}
            extraEntries={extraEntries}
            historyLoading={historyLoading}
            historyError={historyError}
            // 任务结束后没有「下一步」可以留言了：不放一个禁用的输入框
            footer={!active ? undefined :
              <ConsoleComposer
                voyageId={id}
                // 提问已经在页面上方回答；这里只留「给 AI 留一句话」
                openAsk={null}
                disabled={VOYAGE_TERMINAL.has(voyage.status)}
                inputRef={composerRef}
              />
            }
          />
        )}
      </div>
    </div>
  );
}
