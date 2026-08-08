import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { GateCard } from '../../components/ui/GateCard';
import { Segmented } from '../../components/ui/Segmented';
import { SysinfoPanel } from '../../components/ui/SysinfoPanel';
import { toast } from '../../components/ui/Toast';
import { useShell } from '../../app/AppShell';
import { subscribeSse } from '../../lib/sse';
import { tr } from '../../lib/i18n';
import {
  api,
  EXPERIMENT_TERMINAL,
  VOYAGE_TERMINAL,
  type ExperimentDetail,
  type GateDecision,
} from '../../lib/api';
import { ActivityBar } from '../voyages/shared/ActivityBar';
import { StepCard } from '../voyages/shared/StepCard';
import { byListOrder } from '../voyages/shared/stepUtils';
import { TaskTerminal, type TerminalExtraEntry } from '../voyages/shared/terminal';
import { useVoyageChannel } from '../voyages/shared/useVoyageChannel';
import { openAskOf, useVoyageMessages } from '../voyages/shared/useVoyageMessages';
import { budgetText } from './shared';
import { ConsoleComposer } from '../voyages/shared/ConsoleComposer';
import { messageNode } from '../voyages/shared/AskBlock';
import { TaskMap } from './TaskMap';

/* ============================================================
   运行台（console tab）：实验的 AI 任务控制台。
   ActivityBar（状态一句话 + 暂停态操作）
   → 任务地图（横向节点带，点选看详情）
   → 选中节点详情 / 概览（服务器状态 + 进度 + 预算）
   → 深色终端（AI 过程日志 + 对话混排；可切脚本输出 / 仅看对话）
   → 对话输入框（建议随时发；AI 提问时高亮等回复）。
   ============================================================ */

const MAX_SCRIPT_LINES = 2000;

/** SSE data → 脚本日志行（兼容 JSON {line}/{lines} 与纯文本）。 */
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

/** 脚本输出（run.log）：与 AI 过程终端同一深色外观的简单日志框。 */
function ScriptLogView({ expId, active }: { expId: string; active: boolean }) {
  const [lines, setLines] = useState<string[]>([]);
  const [truncated, setTruncated] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setLines([]);
    api
      .getExperimentLogs(expId, { tail: 500 })
      .then((r) => {
        if (cancelled) return;
        setLines(r.lines.slice(-MAX_SCRIPT_LINES));
        setTruncated(r.truncated);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [expId]);

  useEffect(() => {
    if (!active) return;
    const stop = subscribeSse(`/experiments/${expId}/logs/stream`, {
      onEvent: (_event, data) => {
        const next = parseLogEvent(data);
        if (next.length > 0) setLines((l) => [...l, ...next].slice(-MAX_SCRIPT_LINES));
      },
    });
    return stop;
  }, [expId, active]);

  useEffect(() => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lines]);

  return (
    <div
      ref={boxRef}
      className="scroll"
      style={{
        height: 420,
        overflowY: 'auto',
        background: 'var(--terminal-bg)',
        border: '0.5px solid var(--terminal-border)',
        borderRadius: 12,
        padding: '12px 14px',
        fontFamily: 'var(--mono)',
        fontSize: 11.5,
        lineHeight: 1.65,
        color: 'var(--terminal-fg)',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
      }}
    >
      {truncated && (
        <div style={{ color: 'var(--terminal-dim)' }}>
          {tr('…（更早日志已截断，仅显示尾部）', '… (earlier log truncated, showing the tail)')}
        </div>
      )}
      {lines.length === 0 ? (
        <div style={{ color: 'var(--terminal-dim)' }}>
          {tr('暂无脚本输出（尚未开始运行）', 'No script output yet (not running)')}
        </div>
      ) : (
        lines.map((l, i) => <div key={i}>{l}</div>)
      )}
    </div>
  );
}

/** 概览面板（未选中节点时）：服务器状态 + 预算 + 任务用量。 */
function OverviewPanel({ exp }: { exp: ExperimentDetail }) {
  const sysActive = !EXPERIMENT_TERMINAL.has(exp.status);
  const sysinfo = useQuery({
    queryKey: ['experiment', exp.id, 'sysinfo'],
    queryFn: () => api.getExperimentSysinfo(exp.id),
    retry: false,
    refetchInterval: sysActive ? 20_000 : false,
  });
  return (
    <div className="card card-pad">
      <div className="row gap8" style={{ marginBottom: 10, flexWrap: 'wrap' }}>
        <span className="section-h">
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          {tr('服务器状态', 'Server status')}
          {exp.server_host && <span className="mono muted" style={{ fontSize: 11 }}>{exp.server_host}</span>}
        </span>
        <span className="pill sm mono" style={{ marginLeft: 'auto', background: 'var(--surface-3)' }}>
          {budgetText(exp.budget)}
        </span>
      </div>
      <SysinfoPanel
        loading={sysinfo.isLoading}
        error={sysinfo.isError}
        info={sysinfo.data}
        onRefresh={() => void sysinfo.refetch()}
      />
    </div>
  );
}

export function ConsoleTab({ exp }: { exp: ExperimentDetail }) {
  const { openGates } = useShell();
  const queryClient = useQueryClient();
  const vid = exp.voyage_id;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [source, setSource] = useState<'ai' | 'script'>('ai');
  const [chatOnly, setChatOnly] = useState(false);
  const [showObsolete, setShowObsolete] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

  const { data: voyage } = useQuery({
    queryKey: ['voyage', vid, showObsolete],
    queryFn: () => api.getVoyage(vid!, { includeObsolete: showObsolete }),
    enabled: !!vid,
    retry: false,
  });
  const voyageActive = !!voyage && !VOYAGE_TERMINAL.has(voyage.status);

  const { messages, handleExtraEvent } = useVoyageMessages(vid);
  const { terminal, live, clearTerminal, historyLoading, historyError } = useVoyageChannel(
    vid,
    voyageActive,
    { onExtraEvent: handleExtraEvent },
  );

  const openAsk = openAskOf(voyage, messages);

  const resumeMutation = useMutation({
    mutationFn: () => api.resumeVoyage(vid!),
    onSuccess: () => {
      toast(tr('已重新入队，从断点续跑', 'Re-queued — resuming from where it stopped'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyage', vid] });
    },
    onError: (e) =>
      toast(`${tr('重试失败：', 'Retry failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // paused_gate：就地审批（全局抽屉仍可用，openGates 兜底）
  const gates = useQuery({
    queryKey: ['gates', 'pending', exp.project_id],
    queryFn: () => api.listGates('pending', exp.project_id),
    enabled: voyage?.status === 'paused_gate',
    retry: false,
  });
  const inlineGate = (gates.data ?? []).find((g) => g.payload?.voyage_id === vid);
  const decideMutation = useMutation({
    mutationFn: ({ id, decision, comment }: { id: string; decision: GateDecision; comment?: string }) =>
      api.decideGate(id, decision, comment),
    onSuccess: () => {
      toast(tr('已提交审批结果', 'Decision submitted'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
      void queryClient.invalidateQueries({ queryKey: ['voyage', vid] });
      void queryClient.invalidateQueries({ queryKey: ['experiment'] });
    },
    onError: (e) =>
      toast(`${tr('审批失败：', 'Decision failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const steps = useMemo(() => [...(voyage?.steps ?? [])].sort(byListOrder), [voyage?.steps]);
  const planEvents = voyage?.plan_history ?? [];
  const selectedStep = steps.find((s) => s.id === selectedId) ?? null;
  const planAdjusted = (voyage?.plan_iteration ?? 0) > 0;

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

  if (!vid) {
    return (
      <div className="card">
        <EmptyState
          compact
          icon="server"
          title={tr('该实验没有关联 AI 任务', 'This experiment has no linked AI task')}
          desc={tr('早期实验数据，无法使用运行台。', 'Legacy experiment — the console is unavailable.')}
        />
      </div>
    );
  }

  return (
    <div className="fadeup col gap16">
      {/* 当前活动 + 暂停态操作 */}
      {voyage && (
        <div className="card card-pad">
          <ActivityBar
            voyage={voyage}
            steps={steps}
            onOpenGates={() => openGates(inlineGate?.id ?? null)}
            onResume={() => resumeMutation.mutate()}
            resuming={resumeMutation.isPending}
            onReply={focusComposer}
          />
          {voyage.status === 'paused_gate' && inlineGate && (
            <div style={{ marginTop: 12 }}>
              <GateCard
                gate={inlineGate}
                expanded
                onToggle={() => undefined}
                deciding={decideMutation.isPending}
                onDecide={(id, decision, comment) => decideMutation.mutate({ id, decision, comment })}
              />
            </div>
          )}
        </div>
      )}

      {/* 任务地图 */}
      <div className="card" style={{ padding: '10px 14px' }}>
        <div className="row gap8" style={{ flexWrap: 'wrap' }}>
          <span className="section-h">
            <Icon name="compass" size={15} style={{ color: 'var(--accent)' }} />
            {tr('任务地图', 'Task map')}
          </span>
          {planAdjusted && (
            <label
              className="row gap6"
              style={{ marginLeft: 'auto', fontSize: 11.5, color: 'var(--text-3)', cursor: 'pointer', userSelect: 'none' }}
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
        <TaskMap
          steps={steps}
          planEvents={planEvents}
          voyageStatus={voyage?.status ?? null}
          askStepId={openAsk?.step_id ?? null}
          selectedId={selectedId}
          onSelect={setSelectedId}
          showObsolete={showObsolete}
        />
      </div>

      {/* 选中节点详情 / 概览 */}
      {selectedStep ? (
        <div className="col gap8">
          <div className="row">
            <span className="section-h">
              <Icon name="layers" size={15} style={{ color: 'var(--accent)' }} />
              {tr('步骤详情', 'Step detail')}
            </span>
            <button className="btn btn-ghost sm" style={{ marginLeft: 'auto' }} onClick={() => setSelectedId(null)}>
              <Icon name="x" size={12} />
              {tr('收起', 'Close')}
            </button>
          </div>
          <StepCard step={selectedStep} planEvents={planEvents} />
        </div>
      ) : (
        <OverviewPanel exp={exp} />
      )}

      {/* 终端（AI 过程 / 脚本输出）+ 对话输入框 */}
      <div>
        {source === 'ai' ? (
          <TaskTerminal
            state={terminal}
            live={live}
            onClear={clearTerminal}
            height={420}
            extraEntries={extraEntries}
            historyLoading={historyLoading}
            historyError={historyError}
            filter={chatOnly ? () => false : undefined}
            headerExtras={
              <>
                <label
                  className="row gap6"
                  style={{ fontSize: 11.5, color: 'var(--text-3)', cursor: 'pointer', userSelect: 'none' }}
                >
                  <input type="checkbox" checked={chatOnly} onChange={(e) => setChatOnly(e.target.checked)} />
                  {tr('仅看对话', 'Chat only')}
                </label>
                <Segmented
                  value={source}
                  onChange={(v) => setSource(v as 'ai' | 'script')}
                  options={[
                    { v: 'ai' as const, label: tr('AI 过程', 'AI process') },
                    { v: 'script' as const, label: tr('脚本输出', 'Script output') },
                  ]}
                />
              </>
            }
            footer={
              <ConsoleComposer
                voyageId={vid}
                openAsk={openAsk}
                disabled={!!voyage && VOYAGE_TERMINAL.has(voyage.status)}
                inputRef={composerRef}
              />
            }
          />
        ) : (
          <>
            <div className="row" style={{ margin: '20px 0 12px' }}>
              <span className="section-h">
                <Icon name="cpu" size={15} style={{ color: 'var(--accent)' }} />
                {tr('运行日志', 'Terminal')}
                <span className="en-label" style={{ fontSize: 11 }}>run.log</span>
              </span>
              <div className="row gap8" style={{ marginLeft: 'auto' }}>
                <Segmented
                  value={source}
                  onChange={(v) => setSource(v as 'ai' | 'script')}
                  options={[
                    { v: 'ai' as const, label: tr('AI 过程', 'AI process') },
                    { v: 'script' as const, label: tr('脚本输出', 'Script output') },
                  ]}
                />
              </div>
            </div>
            <ScriptLogView expId={exp.id} active={!EXPERIMENT_TERMINAL.has(exp.status)} />
            <ConsoleComposer
              voyageId={vid}
              openAsk={openAsk}
              disabled={!!voyage && VOYAGE_TERMINAL.has(voyage.status)}
              inputRef={composerRef}
            />
          </>
        )}
      </div>
    </div>
  );
}
