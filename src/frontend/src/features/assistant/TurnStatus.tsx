import { useEffect, useRef, useState } from 'react';
import { PolarisMark } from '../../components/ui/PolarisLogo';
import type { AssistantBlock } from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';
import { toolDisplayName } from './agentTools';

/* ============================================================
   一轮对话的「现在在干什么」。

   状态**只从已收到的块推**，不另立一套状态机——界面说的和流里发生的是同一件事，
   不会出现"显示正在检索、其实早就在写答案了"（#249 的老毛病就是两套状态各说各话）。

   五态：
     approval  外部 agent 在等你批准（有没定下来的授权卡），它就停在这儿
     thinking  收到过 thinking、还没工具也没正文
     tool      最后一个块是 running 的工具
     writing   正文正在流
     idle      本轮结束
   ============================================================ */

export type TurnPhase = 'approval' | 'thinking' | 'tool' | 'writing' | 'idle';

export function phaseOf(blocks: AssistantBlock[], busy: boolean): TurnPhase {
  if (!busy) return 'idle';
  // 等批准压过一切：工具卡此刻也显示 running，但真正卡住它的是你
  if (blocks.some((b) => b.kind === 'permission' && (b.state === 'pending' || b.state === 'answering'))) {
    return 'approval';
  }
  // 定下来的授权卡不算「在干什么」：批准之后它接着跑的是卡片前面那个工具
  const last = blocks.filter((b) => b.kind !== 'permission').at(-1);
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
    phase === 'approval'
      ? tr('等你批准', 'Waiting for your approval')
      : phase === 'tool'
        ? tr(
            `正在使用：${running.map((b) => (b.kind === 'tool' ? toolDisplayName(b) : '')).join('、')}`,
            `Using: ${running.map((b) => (b.kind === 'tool' ? toolDisplayName(b) : '')).join(', ')}`,
          )
        : phase === 'writing'
          ? tr('正在回答', 'Writing')
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
        {phase === 'approval' ? label : stepLabel || label}
      </span>
      {secs >= 3 && (
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
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
