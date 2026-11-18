import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { SysinfoPanel } from '../../components/ui/SysinfoPanel';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { subscribeSse } from '../../lib/sse';
import { tr } from '../../lib/i18n';
import { api, EXPERIMENT_TERMINAL, VOYAGE_TERMINAL, type ExperimentDetail, type VoyageDetail } from '../../lib/api';
import { messageNode } from '../voyages/shared/AskBlock';
import { ConsoleComposer } from '../voyages/shared/ConsoleComposer';
import { PlanEventCard, StepCard } from '../voyages/shared/StepCard';
import { buildTimelineEntries, byListOrder, stepMarker } from '../voyages/shared/stepUtils';
import { TaskTerminal, type TerminalExtraEntry, type TerminalState } from '../voyages/shared/terminal';
import type { VoyageMessageRead } from '../../lib/api';

/* ============================================================
   实验的「进展」：和任务页同一种读法。

   · 步骤：每步一行名字 + 状态 + 一句结果，细节在「详情」里；
   · 日志默认收起。点开后可以在三种输出之间切换（AI 的过程 / 程序输出 / 命令输出），
     也可以给 AI 留一句话；
   · 机器状态默认收起，点开才去连那台机器（以前一进页面就连，机器连不上时整页跟着等）。

   AI 的提问不在这里——它在页面最上面的那张卡片里回答。
   ============================================================ */

const MAX_SCRIPT_LINES = 2000;

/** SSE data → 日志行（兼容 JSON {line}/{lines} 与纯文本）。 */
function parseLogEvent(data: string): string[] {
  try {
    const p: unknown = JSON.parse(data);
    if (p && typeof p === 'object') {
      const rec = p as { line?: unknown; lines?: unknown; message?: unknown };
      if (typeof rec.line === 'string') return [rec.line];
      if (Array.isArray(rec.lines)) return rec.lines.filter((l): l is string => typeof l === 'string');
      if (typeof rec.message === 'string') return [rec.message];
      return [];
    }
    if (typeof p === 'string') return [p];
  } catch {
    /* 非 JSON：按纯文本处理 */
  }
  return data.split('\n').filter((l) => l !== '');
}

/** 程序或远程命令的输出。 */
function RemoteLogView({ expId, active, source }: { expId: string; active: boolean; source: 'script' | 'terminal' }) {
  const [lines, setLines] = useState<string[]>([]);
  const [truncated, setTruncated] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setLines([]);
    const initial = source === 'script' ? api.getExperimentLogs(expId, { tail: 500 }) : api.getExperimentTerminalLogs(expId, 500);
    initial
      .then((r) => {
        if (cancelled) return;
        setLines(r.lines.slice(-MAX_SCRIPT_LINES));
        setTruncated(r.truncated);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [expId, source]);

  useEffect(() => {
    if (!active) return;
    const endpoint = source === 'script' ? 'logs' : 'terminal-logs';
    return subscribeSse(`/experiments/${expId}/${endpoint}/stream`, {
      onEvent: (_event, data) => {
        const next = parseLogEvent(data);
        if (next.length > 0) setLines((l) => [...l, ...next].slice(-MAX_SCRIPT_LINES));
      },
    });
  }, [expId, active, source]);

  useEffect(() => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lines]);

  return (
    <div ref={boxRef} className="scroll exp-remote-log">
      {truncated && <div className="exp-remote-dim">{tr('… 只显示最近的输出', '… Showing the latest output only')}</div>}
      {lines.length === 0 ? (
        <div className="exp-remote-dim">{source === 'script' ? tr('还没有程序输出', 'No program output yet') : tr('还没有命令输出', 'No command output yet')}</div>
      ) : (
        lines.map((l, i) => <div key={i}>{l}</div>)
      )}
    </div>
  );
}

function MachineStatus({ exp }: { exp: ExperimentDetail }) {
  const active = !EXPERIMENT_TERMINAL.has(exp.status);
  const sysinfo = useQuery({
    queryKey: ['experiment', exp.id, 'sysinfo'],
    queryFn: () => api.getExperimentSysinfo(exp.id),
    retry: false,
    refetchInterval: active ? 20_000 : false,
  });
  return <SysinfoPanel loading={sysinfo.isLoading} error={sysinfo.isError} info={sysinfo.data} onRefresh={() => void sysinfo.refetch()} />;
}

type LogSource = 'ai' | 'script' | 'terminal';

export interface ProgressTabProps {
  exp: ExperimentDetail;
  voyage: VoyageDetail | undefined;
  messages: VoyageMessageRead[];
  terminal: TerminalState;
  live: boolean;
  clearTerminal: () => void;
  historyLoading: boolean;
  historyError: boolean;
  showObsolete: boolean;
  onShowObsolete: (v: boolean) => void;
}

export function ProgressTab({ exp, voyage, messages, terminal, live, clearTerminal, historyLoading, historyError, showObsolete, onShowObsolete }: ProgressTabProps) {
  const [showLog, setShowLog] = useState(false);
  const [showMachine, setShowMachine] = useState(false);
  const [source, setSource] = useState<LogSource>('ai');
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

  const steps = useMemo(() => [...(voyage?.steps ?? [])].sort(byListOrder), [voyage?.steps]);
  const planEvents = voyage?.plan_history ?? [];
  const entries = buildTimelineEntries(steps, showObsolete ? planEvents : []);
  const planAdjusted = (voyage?.plan_iteration ?? 0) > 0;
  const voyageActive = !!voyage && !VOYAGE_TERMINAL.has(voyage.status);

  const extraEntries = useMemo<TerminalExtraEntry[]>(() => {
    const out: TerminalExtraEntry[] = [];
    for (const m of messages) {
      const node = messageNode(m);
      if (node) out.push({ id: m.id, at: m.created_at, node });
    }
    return out;
  }, [messages]);

  if (!exp.voyage_id) {
    return <div className="task-empty">{tr('这个实验是较早创建的，没有记录步骤。', 'This is an older experiment; its steps weren’t recorded.')}</div>;
  }

  const sourcePicker = (
    <Segmented<LogSource>
      value={source}
      onChange={setSource}
      options={[
        { v: 'ai', label: tr('AI 的过程', 'AI log') },
        { v: 'script', label: tr('程序输出', 'Program output') },
        { v: 'terminal', label: tr('命令输出', 'Command output') },
      ]}
    />
  );
  const composer = voyageActive ? (
    // 提问已经在页面上方回答；这里只留「给 AI 留一句话」
    <ConsoleComposer voyageId={exp.voyage_id} openAsk={null} disabled={false} inputRef={composerRef} />
  ) : undefined;

  return (
    <div data-exp-tab="progress">
      <div className="task-sec-head" style={{ marginTop: 0 }}>
        <h2 className="task-sec-title">{tr('步骤', 'Steps')}</h2>
        {planAdjusted && (
          <label className="task-sec-aside">
            <input type="checkbox" checked={showObsolete} onChange={(e) => onShowObsolete(e.target.checked)} />
            {tr('显示计划的调整', 'Show plan changes')}
          </label>
        )}
      </div>
      {entries.length === 0 ? (
        <div className="task-empty">{voyage ? tr('正在安排步骤…', 'Working out the steps…') : tr('加载中…', 'Loading…')}</div>
      ) : (
        <Timeline>
          {entries.map((entry, i) => {
            const last = i === entries.length - 1;
            if (entry.kind === 'plan') {
              return (
                <TimelineItem key={`plan-${entry.event.iteration}`} marker={<Icon name="refresh" size={12} />} markerBg="var(--surface-2)" markerColor="var(--text-3)" last={last}>
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

      <div className="task-log">
        <button type="button" className="btn btn-ghost sm" data-log-toggle aria-expanded={showLog} onClick={() => setShowLog((s) => !s)}>
          <Icon name="chevDown" size={12} style={{ transform: showLog ? 'rotate(180deg)' : 'none' }} />
          {showLog ? tr('隐藏日志', 'Hide log') : tr('显示日志', 'Show log')}
        </button>
        <button type="button" className="btn btn-ghost sm" data-machine-toggle aria-expanded={showMachine} onClick={() => setShowMachine((s) => !s)}>
          <Icon name="chevDown" size={12} style={{ transform: showMachine ? 'rotate(180deg)' : 'none' }} />
          {showMachine ? tr('隐藏机器状态', 'Hide machine status') : tr('机器状态', 'Machine status')}
        </button>
        {!showLog && voyageActive && (
          <span className="task-log-hint">{tr('日志里可以给 AI 留一句话，它会在下一步之前看到。', 'In the log you can leave a note for the AI. It reads it before the next step.')}</span>
        )}
        {showMachine && (
          <div className="exp-machine">
            <div className="exp-machine-head">
              {tr('实验机器', 'Experiment machine')}
              {exp.server_host && <span>{exp.server_host}</span>}
            </div>
            <MachineStatus exp={exp} />
          </div>
        )}
        {showLog &&
          (source === 'ai' ? (
            <TaskTerminal
              state={terminal}
              live={live}
              onClear={clearTerminal}
              height={420}
              extraEntries={extraEntries}
              historyLoading={historyLoading}
              historyError={historyError}
              headerExtras={sourcePicker}
              footer={composer}
            />
          ) : (
            <div>
              <div className="exp-log-head">{sourcePicker}</div>
              <RemoteLogView expId={exp.id} active={!EXPERIMENT_TERMINAL.has(exp.status)} source={source} />
              {composer}
            </div>
          ))}
      </div>
    </div>
  );
}
