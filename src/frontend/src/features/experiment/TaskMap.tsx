import { useEffect, useMemo, useRef, useState } from 'react';
import { Icon, type IconName } from '../../components/ui/Icon';
import { tr } from '../../lib/i18n';
import type { VoyagePlanEvent, VoyageStepRead, VoyageStatus } from '../../lib/api';
import { byListOrder } from '../voyages/shared/stepUtils';

/* ============================================================
   任务地图：横向演进的节点带（节点 = voyage 步骤，rank 序）。
   - 计划调整处插旗标分隔；作废步骤淡色划线留在原位（可选显示）；
   - 审批节点菱形、等回答节点黄色 ? 角标、运行中脉冲；
   - 连续通过的长段折叠成「✓ 前 N 步」胶囊；自动滚到当前节点；
   - 自研 flex + 少量样式（仓库惯例：不引图库依赖，颜色全走 token）。
   ============================================================ */

/** action → 图标（experiment.* 有专属图标，其余按前缀兜底）。 */
function actionIcon(action: string): IconName {
  if (action.endsWith('.plan')) return 'compass';
  if (action.endsWith('.setup')) return 'server';
  if (action.endsWith('.smoke')) return 'flask';
  if (action.endsWith('.run')) return 'play';
  if (action.endsWith('.analyze')) return 'sparkle';
  if (action.endsWith('.figures')) return 'chart';
  if (action.endsWith('.report')) return 'pen';
  return 'layers';
}

/** 步骤 → 地图上的短标签（第 N 轮 / 分析 N 用轮次计数，渲染处 tr）。 */
function nodeLabel(step: VoyageStepRead, roundNo: number | null): string {
  const a = step.action;
  if (a.endsWith('.plan')) return tr('计划', 'plan');
  if (a.endsWith('.setup')) return tr('环境', 'setup');
  if (a.endsWith('.smoke')) return tr('试跑', 'smoke');
  if (a.endsWith('.run')) return roundNo ? tr(`第 ${roundNo} 轮`, `run ${roundNo}`) : tr('运行', 'run');
  if (a.endsWith('.analyze')) return roundNo ? tr(`分析 ${roundNo}`, `analyze ${roundNo}`) : tr('分析', 'analyze');
  if (a.endsWith('.figures')) return tr('图表', 'figures');
  if (a.endsWith('.report')) return tr('报告', 'report');
  return step.title.length > 6 ? `${step.title.slice(0, 6)}…` : step.title;
}

type NodeVisual = 'pending' | 'running' | 'passed' | 'failed' | 'obsolete' | 'ask-wait';

function stepVisual(step: VoyageStepRead, askStepId: string | null): NodeVisual {
  if (step.status === 'obsolete') return 'obsolete';
  if (askStepId && step.id === askStepId) return 'ask-wait';
  if (step.status === 'running' || step.status === 'verifying') return 'running';
  if (step.status === 'passed') return 'passed';
  if (step.status === 'failed') return 'failed';
  return 'pending';
}

const VISUAL_STYLE: Record<NodeVisual, { bg: string; color: string; border: string }> = {
  pending: { bg: 'var(--surface-2)', color: 'var(--text-3)', border: '1px dashed var(--border)' },
  running: { bg: 'var(--accent)', color: '#fff', border: '1px solid var(--accent)' },
  passed: { bg: 'var(--ok-bg)', color: 'var(--ok-tx)', border: '1px solid transparent' },
  failed: { bg: 'var(--danger-bg)', color: 'var(--danger-tx)', border: '1px solid transparent' },
  obsolete: { bg: 'var(--surface-3)', color: 'var(--text-4)', border: '1px solid transparent' },
  'ask-wait': { bg: 'var(--warn-bg)', color: 'var(--warn-tx)', border: '1px solid var(--warn-tx)' },
};

type MapItem =
  | { kind: 'step'; step: VoyageStepRead; roundNo: number | null }
  | { kind: 'plan-event'; event: VoyagePlanEvent }
  | { kind: 'collapsed'; steps: VoyageStepRead[] };

/** 连续 passed 段超过该长度时折叠（保尾部一个不折，衔接当前进度更直观）。 */
const COLLAPSE_MIN = 6;

export function buildTaskMapItems(
  steps: VoyageStepRead[],
  planEvents: VoyagePlanEvent[],
  opts?: { showObsolete?: boolean; collapse?: boolean },
): MapItem[] {
  const showObsolete = opts?.showObsolete ?? false;
  const collapse = opts?.collapse ?? true;
  const ordered = [...steps].sort(byListOrder).filter((s) => showObsolete || s.status !== 'obsolete');
  const byIteration = new Map(planEvents.map((e) => [e.iteration, e]));
  const inserted = new Set<number>();

  // 轮次计数：run/analyze 各自按出现顺序编号（obsolete 不计）
  let runNo = 0;
  let analyzeNo = 0;
  const items: MapItem[] = [];
  for (const step of ordered) {
    const iter = step.provenance?.plan_iteration ?? 0;
    if (iter > 0 && !inserted.has(iter) && byIteration.has(iter)) {
      inserted.add(iter);
      items.push({ kind: 'plan-event', event: byIteration.get(iter)! });
    }
    let roundNo: number | null = null;
    if (step.status !== 'obsolete') {
      if (step.action.endsWith('.run')) roundNo = ++runNo;
      else if (step.action.endsWith('.analyze')) roundNo = ++analyzeNo;
    }
    items.push({ kind: 'step', step, roundNo });
  }

  if (!collapse) return items;
  // 折叠连续 passed 步骤段（只折 step 项；plan-event 分隔天然切段）
  const out: MapItem[] = [];
  let buffer: Extract<MapItem, { kind: 'step' }>[] = [];
  const flush = () => {
    if (buffer.length > COLLAPSE_MIN) {
      const keepTail = buffer[buffer.length - 1]!;
      out.push({ kind: 'collapsed', steps: buffer.slice(0, -1).map((b) => b.step) });
      out.push(keepTail);
    } else {
      out.push(...buffer);
    }
    buffer = [];
  };
  for (const item of items) {
    if (item.kind === 'step' && item.step.status === 'passed') {
      buffer.push(item);
    } else {
      flush();
      out.push(item);
    }
  }
  flush();
  return out;
}

function Connector() {
  return (
    <span
      aria-hidden
      style={{ width: 14, height: 1.5, background: 'var(--border)', flexShrink: 0, alignSelf: 'center', marginBottom: 18 }}
    />
  );
}

function StepNode({
  step,
  roundNo,
  selected,
  visual,
  onSelect,
  nodeRef,
}: {
  step: VoyageStepRead;
  roundNo: number | null;
  selected: boolean;
  visual: NodeVisual;
  onSelect: (id: string | null) => void;
  nodeRef?: (el: HTMLButtonElement | null) => void;
}) {
  const style = VISUAL_STYLE[visual];
  const gate = !!step.requires_gate;
  const obsolete = visual === 'obsolete';
  return (
    <button
      ref={nodeRef}
      onClick={() => onSelect(selected ? null : step.id)}
      title={`${step.title}${step.attempt > 1 ? tr(`（第 ${step.attempt} 次尝试）`, ` (attempt ${step.attempt})`) : ''}`}
      className="col"
      style={{
        border: 'none',
        background: 'transparent',
        cursor: 'pointer',
        padding: '2px 2px 0',
        alignItems: 'center',
        gap: 5,
        flexShrink: 0,
        width: 62,
      }}
    >
      <span
        className={visual === 'running' ? 'pulse' : undefined}
        style={{
          position: 'relative',
          width: 34,
          height: 34,
          borderRadius: gate ? 9 : 999,
          transform: gate ? 'rotate(45deg)' : 'none',
          background: style.bg,
          color: style.color,
          border: style.border,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          boxShadow: selected ? '0 0 0 2.5px var(--accent-soft), 0 0 0 4px var(--accent)' : 'none',
          opacity: obsolete ? 0.55 : 1,
        }}
      >
        <span style={{ transform: gate ? 'rotate(-45deg)' : 'none', display: 'flex' }}>
          <Icon name={gate ? 'gate' : actionIcon(step.action)} size={15} />
        </span>
        {visual === 'ask-wait' && (
          <span
            className="pulse"
            style={{
              position: 'absolute',
              top: -5,
              right: -5,
              width: 15,
              height: 15,
              borderRadius: 999,
              background: 'var(--warn-tx)',
              color: '#fff',
              fontSize: 10,
              fontWeight: 800,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              transform: gate ? 'rotate(-45deg)' : 'none',
            }}
          >
            ?
          </span>
        )}
        {visual === 'failed' && (
          <span
            style={{
              position: 'absolute',
              top: -4,
              right: -4,
              width: 13,
              height: 13,
              borderRadius: 999,
              background: 'var(--danger-tx)',
              color: '#fff',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              transform: gate ? 'rotate(-45deg)' : 'none',
            }}
          >
            <Icon name="x" size={9} />
          </span>
        )}
      </span>
      <span
        style={{
          fontSize: 10.5,
          lineHeight: 1.2,
          color: selected ? 'var(--text)' : 'var(--text-3)',
          fontWeight: selected ? 700 : 500,
          textDecoration: obsolete ? 'line-through' : 'none',
          maxWidth: 62,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {nodeLabel(step, roundNo)}
      </span>
    </button>
  );
}

export function TaskMap({
  steps,
  planEvents,
  voyageStatus,
  askStepId,
  selectedId,
  onSelect,
  showObsolete,
}: {
  steps: VoyageStepRead[];
  planEvents: VoyagePlanEvent[];
  voyageStatus: VoyageStatus | null;
  /** 等回答的提问挂在哪个步骤上（该节点顶黄色 ? 角标） */
  askStepId: string | null;
  selectedId: string | null;
  onSelect: (stepId: string | null) => void;
  showObsolete?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const focusRef = useRef<HTMLButtonElement | null>(null);

  const items = useMemo(
    () => buildTaskMapItems(steps, planEvents, { showObsolete, collapse: !expanded }),
    [steps, planEvents, showObsolete, expanded],
  );

  // 当前焦点节点（运行中 / 等回答 / 失败）居中；节点数变化时重新对齐
  useEffect(() => {
    focusRef.current?.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
  }, [items.length, voyageStatus]);

  if (steps.length === 0) {
    return (
      <div className="card empty" style={{ padding: 22, fontSize: 12.5 }}>
        {voyageStatus === 'planning'
          ? tr('AI 正在规划步骤…', 'AI is planning the steps…')
          : tr('暂无步骤', 'No steps yet')}
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      className="scroll"
      style={{ overflowX: 'auto', padding: '10px 4px 4px', display: 'flex', alignItems: 'flex-start' }}
    >
      {items.map((item, i) => {
        const connector = i > 0 ? <Connector key={`c-${i}`} /> : null;
        if (item.kind === 'plan-event') {
          return (
            <span key={`p-${item.event.iteration}`} className="row" style={{ flexShrink: 0 }}>
              {connector}
              <span
                className="col"
                title={item.event.reason || tr('计划调整', 'Plan adjusted')}
                style={{ alignItems: 'center', gap: 5, width: 44, paddingTop: 2 }}
              >
                <span
                  style={{
                    width: 26,
                    height: 34,
                    borderRadius: 8,
                    background: 'var(--accent-soft)',
                    color: 'var(--accent-text)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <Icon name="refresh" size={13} />
                </span>
                <span style={{ fontSize: 10, color: 'var(--accent-text)', whiteSpace: 'nowrap' }}>
                  {tr(`调整 ${item.event.iteration}`, `adj ${item.event.iteration}`)}
                </span>
              </span>
            </span>
          );
        }
        if (item.kind === 'collapsed') {
          return (
            <span key={`col-${i}`} className="row" style={{ flexShrink: 0 }}>
              {connector}
              <button
                onClick={() => setExpanded(true)}
                title={tr('展开这些已完成的步骤', 'Expand these completed steps')}
                className="col"
                style={{
                  border: 'none',
                  background: 'transparent',
                  cursor: 'pointer',
                  alignItems: 'center',
                  gap: 5,
                  padding: '2px 2px 0',
                  flexShrink: 0,
                }}
              >
                <span
                  style={{
                    height: 34,
                    padding: '0 12px',
                    borderRadius: 999,
                    background: 'var(--ok-bg)',
                    color: 'var(--ok-tx)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 5,
                    fontSize: 11,
                    fontWeight: 700,
                  }}
                >
                  <Icon name="check" size={12} />
                  {item.steps.length}
                </span>
                <span style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
                  {tr(`${item.steps.length} 步已完成`, `${item.steps.length} done`)}
                </span>
              </button>
            </span>
          );
        }
        const visual = stepVisual(item.step, askStepId);
        const isFocus =
          visual === 'running' || visual === 'ask-wait' || (visual === 'failed' && voyageStatus !== 'done');
        return (
          <span key={item.step.id} className="row" style={{ flexShrink: 0 }}>
            {connector}
            <StepNode
              step={item.step}
              roundNo={item.roundNo}
              selected={selectedId === item.step.id}
              visual={visual}
              onSelect={onSelect}
              nodeRef={isFocus ? (el) => (focusRef.current = el) : undefined}
            />
          </span>
        );
      })}
    </div>
  );
}
