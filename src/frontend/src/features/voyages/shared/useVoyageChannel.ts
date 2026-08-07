import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { subscribeSse } from '../../../lib/sse';
import { useTaskLogHistory } from '../../../lib/prefs';
import {
  api,
  VOYAGE_TERMINAL,
  type VoyageDetail,
  type VoyageStatus,
  type VoyageStepRead,
} from '../../../lib/api';
import { byListOrder } from './stepUtils';
import {
  LOG_LEVELS,
  TERMINAL_MAX,
  type LogLevel,
  type TerminalEntry,
  type TerminalState,
} from './terminal';

/* ============================================================
   任务实时通道 hook：SSE 订阅 + 终端缓冲（ref 累积 + 80ms 节流 flush）
   + 历史日志回放 + status/step 事件与 TanStack Query 缓存合并。
   从 VoyageDetailPage 抽出，供任务详情页与实验运行台共用。
   已知事件（status/step/log/llm_*）内部处理；未知事件（如 message /
   ask.created）经 onExtraEvent 透传给调用方。
   ============================================================ */

export function useVoyageChannel(
  voyageId: string | null,
  active: boolean,
  opts?: { onExtraEvent?: (event: string, data: unknown) => void },
): { terminal: TerminalState; live: boolean; clearTerminal: () => void } {
  const id = voyageId ?? '';
  const queryClient = useQueryClient();
  const [live, setLive] = useState(false);

  // —— 终端状态：ref 累积 + 节流 setState，避免高频 delta / 批处理日志每段一次重渲染 ——
  const [terminal, setTerminal] = useState<TerminalState>({ entries: [], active: null });
  const termBufRef = useRef<TerminalState>({ entries: [], active: null });
  const termIdRef = useRef(0);
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 回调走 ref：调用方每次渲染传新函数也不触发重新订阅
  const onExtraEventRef = useRef(opts?.onExtraEvent);
  onExtraEventRef.current = opts?.onExtraEvent;

  const flushTerminal = useCallback(() => {
    flushTimerRef.current = null;
    const buf = termBufRef.current;
    setTerminal({ entries: buf.entries.slice(), active: buf.active ? { ...buf.active } : null });
  }, []);
  const scheduleTermFlush = useCallback(() => {
    if (flushTimerRef.current != null) return;
    flushTimerRef.current = setTimeout(flushTerminal, 80);
  }, [flushTerminal]);
  const clearTerminal = useCallback(() => {
    termBufRef.current = { entries: [], active: null };
    setTerminal({ entries: [], active: null });
  }, []);

  // 切换到别的任务详情时重置终端（同组件换 id 不会重挂载）。
  useEffect(() => {
    termBufRef.current = { entries: [], active: null };
    setTerminal({ entries: [], active: null });
  }, [id]);

  // —— 历史日志回放：刷新后 / 打开已结束任务时，从后端拉持久化日志回填终端 ——
  const showHistory = useTaskLogHistory();
  const { data: logHistory } = useQuery({
    queryKey: ['voyage-logs', id],
    queryFn: () => api.getVoyageLogs(id),
    enabled: !!id && showHistory,
    staleTime: Infinity, // 历史只在挂载 / 切任务时拉一次，实时增量走 SSE
    refetchOnWindowFocus: false,
    retry: false,
  });
  const historyLoadedRef = useRef<string | null>(null);
  useEffect(() => {
    // 每个任务只回填一次；query 按 id 分键，切任务时数据先变 undefined，不会串味。
    if (!logHistory || historyLoadedRef.current === id) return;
    historyLoadedRef.current = id;
    const hist: TerminalEntry[] = logHistory.map((r) =>
      r.event === 'llm'
        ? { kind: 'llm', id: r.id, stage: r.stage ?? '', text: r.message, at: r.at }
        : {
            kind: 'log',
            id: r.id,
            level: (r.level && LOG_LEVELS.has(r.level as LogLevel) ? r.level : 'info') as LogLevel,
            message: r.message,
            at: r.at,
          },
    );
    // 历史在前、已到的实时事件在后；本地 id 计数跳到历史最大 id 之上，避免 React key 撞车。
    const buf = termBufRef.current;
    buf.entries = [...hist, ...buf.entries];
    if (buf.entries.length > TERMINAL_MAX) buf.entries.splice(0, buf.entries.length - TERMINAL_MAX);
    termIdRef.current = logHistory.reduce((m, r) => Math.max(m, r.id), termIdRef.current);
    scheduleTermFlush();
  }, [logHistory, id, scheduleTermFlush]);

  // —— SSE 实时订阅（活动状态时） ——
  useEffect(() => {
    if (!id || !active) return;
    const stop = subscribeSse(`/voyages/${id}/events`, {
      onOpen: () => setLive(true),
      onError: () => setLive(false),
      onEvent: (event, dataStr) => {
        let payload: unknown;
        try {
          payload = JSON.parse(dataStr);
        } catch {
          return;
        }
        if (event === 'status') {
          const p = payload as { status: VoyageStatus; cursor: number | null };
          queryClient.setQueriesData<VoyageDetail>({ queryKey: ['voyage', id] }, (old) =>
            old ? { ...old, status: p.status, cursor: p.cursor ?? old.cursor } : old,
          );
          if (VOYAGE_TERMINAL.has(p.status)) {
            void queryClient.invalidateQueries({ queryKey: ['voyages'] });
            void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
          }
        } else if (event === 'step') {
          const p = payload as { step: VoyageStepRead };
          if (!p.step) return;
          queryClient.setQueriesData<VoyageDetail>({ queryKey: ['voyage', id] }, (old) => {
            if (!old) return old;
            const steps = old.steps ?? [];
            const i = steps.findIndex((s) => s.id === p.step.id);
            const next = i >= 0 ? steps.map((s, j) => (j === i ? p.step : s)) : [...steps, p.step];
            next.sort(byListOrder);
            return { ...old, steps: next };
          });
        } else if (event === 'log') {
          // 向后兼容：老事件可能只有 {message}，level 缺省当 info。
          const p = payload as { message?: string; level?: string; at?: string };
          if (!p.message) return;
          const level = (p.level && LOG_LEVELS.has(p.level as LogLevel) ? p.level : 'info') as LogLevel;
          const buf = termBufRef.current;
          buf.entries.push({
            kind: 'log',
            id: ++termIdRef.current,
            level,
            message: p.message,
            at: p.at ?? new Date().toISOString(),
          });
          if (buf.entries.length > TERMINAL_MAX) buf.entries.splice(0, buf.entries.length - TERMINAL_MAX);
          scheduleTermFlush();
        } else if (event === 'llm_start') {
          const p = payload as { stage?: string };
          termBufRef.current.active = { id: ++termIdRef.current, stage: p.stage ?? '', text: '', at: new Date().toISOString() };
          scheduleTermFlush();
        } else if (event === 'llm_delta') {
          const p = payload as { stage?: string; delta?: string };
          if (!p.delta) return;
          const buf = termBufRef.current;
          // 可能订阅在流中途接上：没有 active 块时惰性补一个。
          if (!buf.active) buf.active = { id: ++termIdRef.current, stage: p.stage ?? '', text: '', at: new Date().toISOString() };
          buf.active.text += p.delta;
          scheduleTermFlush();
        } else if (event === 'llm_end') {
          const buf = termBufRef.current;
          if (buf.active) {
            buf.entries.push({ kind: 'llm', id: buf.active.id, stage: buf.active.stage, text: buf.active.text, at: buf.active.at });
            buf.active = null;
            if (buf.entries.length > TERMINAL_MAX) buf.entries.splice(0, buf.entries.length - TERMINAL_MAX);
            scheduleTermFlush();
          }
        } else {
          onExtraEventRef.current?.(event, payload);
        }
      },
    });
    return () => {
      stop();
      setLive(false);
      if (flushTimerRef.current != null) {
        clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
      }
    };
  }, [id, active, queryClient, scheduleTermFlush]);

  return { terminal, live, clearTerminal };
}
