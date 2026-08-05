import { useEffect, useRef, useState } from 'react';
import { PolarisMark } from '../../components/ui/PolarisLogo';
import type { AssistantBlock } from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';

/* ============================================================
   一轮对话的「现在在干什么」。

   状态**只从已收到的块推**，不另立一套状态机——界面说的和流里发生的是同一件事，
   不会出现"显示正在检索、其实早就在写答案了"（#249 的老毛病就是两套状态各说各话）。

   四态：
     thinking  收到过 thinking、还没工具也没正文
     tool      最后一个块是 running 的工具
     writing   正文正在流
     idle      本轮结束
   ============================================================ */

export type TurnPhase = 'thinking' | 'tool' | 'writing' | 'idle';

export function phaseOf(blocks: AssistantBlock[], busy: boolean): TurnPhase {
  if (!busy) return 'idle';
  const last = blocks[blocks.length - 1];
  if (last?.kind === 'tool' && last.state === 'running') return 'tool';
  if (last?.kind === 'text') return 'writing';
  if (last?.kind === 'thinking') return 'thinking';
  return 'thinking';
}

/** 已跑过的秒数。给长轮次一个"它还活着"的凭据。 */
function useElapsed(active: boolean) {
  const [secs, setSecs] = useState(0);
  const startedAt = useRef<number | null>(null);
  useEffect(() => {
    if (!active) {
      startedAt.current = null;
      setSecs(0);
      return;
    }
    startedAt.current = Date.now();
    const timer = window.setInterval(() => {
      if (startedAt.current) setSecs(Math.round((Date.now() - startedAt.current) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return secs;
}

export function TurnStatus({
  blocks,
  busy,
  onStop,
}: {
  blocks: AssistantBlock[];
  busy: boolean;
  onStop: () => void;
}) {
  const phase = phaseOf(blocks, busy);
  const secs = useElapsed(busy);
  if (phase === 'idle') return null;

  const running = blocks.filter((b) => b.kind === 'tool' && b.state === 'running');
  // 有计划时优先说「在做第几步」——那是用户真正关心的事，工具名只是手段
  const plan = blocks.find((b) => b.kind === 'plan');
  const step =
    plan?.kind === 'plan' ? plan.steps.findIndex((s) => s.status === 'running') : -1;
  const stepLabel =
    plan?.kind === 'plan' && step >= 0
      ? tr(
          `第 ${step + 1}/${plan.steps.length} 步：${plan.steps[step]?.title ?? ''}`,
          `Step ${step + 1}/${plan.steps.length}: ${plan.steps[step]?.title ?? ''}`,
        )
      : '';
  const label =
    phase === 'tool'
      ? tr(
          `正在查：${running.map((b) => (b.kind === 'tool' ? b.name : '')).join('、')}`,
          `Searching: ${running.map((b) => (b.kind === 'tool' ? b.name : '')).join(', ')}`,
        )
      : phase === 'writing'
        ? tr('正在写答案', 'Writing')
        : tr('正在思考', 'Thinking');

  return (
    <div
      className="row gap8"
      style={{
        alignItems: 'center',
        margin: '6px 0',
        fontSize: 12,
        color: 'var(--text-3)',
      }}
    >
      {/* 用标识本身表示「在忙」：转圈是通用的忙，标识差着一点则是「这件事还没做完」 */}
      <span style={{ display: 'inline-flex', animation: 'buddy-breathe 1.6s ease-in-out infinite' }}>
        <PolarisMark size={13} dot={false} />
      </span>
      <span
        style={{ flex: '0 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
      >
        {stepLabel || label}
      </span>
      {secs >= 3 && (
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
          {secs}s
        </span>
      )}
      <span style={{ flex: 1 }} />
      <button className="btn btn-ghost sm" onClick={onStop} style={{ height: 22, fontSize: 11 }}>
        {tr('停止', 'Stop')}
      </button>
    </div>
  );
}
