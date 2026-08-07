import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react';
import { Icon } from '../../../components/ui/Icon';
import { tr } from '../../../lib/i18n';

/* ============================================================
   运行日志终端（Terminal）：结构化日志 + 大模型流式输出。
   深色、等宽、自动滚到底；用户上滑看历史时暂停跟随并给「回到底部」。
   从 VoyageDetailPage 抽出，供任务详情页与实验运行台共用；新增的
   extraEntries / footer / headerExtras / filter / height 都是可选项，
   不传时行为与抽取前完全一致。
   ============================================================ */

export const TERMINAL_MAX = 2500;
const LLM_PREVIEW_LINES = 14;
// 默认直接渲染的最近条目数：超过则把更早的历史折起来（可一键展开），避免长任务几千条 DOM 拖慢渲染。
export const RENDER_WINDOW = 600;

export type LogLevel = 'info' | 'step' | 'success' | 'error' | 'plan' | 'budget' | 'gate';
export const LOG_LEVELS = new Set<LogLevel>(['info', 'step', 'success', 'error', 'plan', 'budget', 'gate']);

/** level → 文字颜色（终端专用变量，深底可读）。 */
const LEVEL_COLOR: Record<LogLevel, string> = {
  info: 'var(--terminal-fg)',
  step: 'var(--terminal-accent)',
  success: 'var(--terminal-ok)',
  error: 'var(--terminal-err)',
  plan: 'var(--terminal-plan)',
  budget: 'var(--terminal-warn)',
  gate: 'var(--terminal-warn)',
};

export interface LogEntry {
  kind: 'log';
  id: number;
  level: LogLevel;
  message: string;
  at: string;
}
export interface LlmEntry {
  kind: 'llm';
  id: number;
  stage: string;
  text: string;
  at: string;
}
export type TerminalEntry = LogEntry | LlmEntry;
export interface ActiveLlm {
  id: number;
  stage: string;
  text: string;
  at: string;
}
export interface TerminalState {
  entries: TerminalEntry[];
  active: ActiveLlm | null;
}

/** 外部注入的终端块（如 AI 提问 / 用户消息）：按 at 时间与日志条目归并渲染。 */
export interface TerminalExtraEntry {
  id: string;
  at: string;
  node: ReactNode;
}

/** stage → 大白话（进行中 / 已完成 两种措辞；模块级常量只存 zh/en，渲染处再 tr）。 */
const STAGE_INFO: Record<string, { activeZh: string; activeEn: string; doneZh: string; doneEn: string }> = {
  navigator: { activeZh: 'AI 正在规划任务', activeEn: 'AI is planning the task', doneZh: 'AI 规划任务', doneEn: 'Task planning' },
  debate: { activeZh: '评审辩论中', activeEn: 'Peer debate in progress', doneZh: '评审辩论', doneEn: 'Peer debate' },
  review: { activeZh: '评审辩论中', activeEn: 'Peer debate in progress', doneZh: '评审辩论', doneEn: 'Peer debate' },
  experiment: { activeZh: '实验分析中', activeEn: 'Analyzing the experiment', doneZh: '实验分析', doneEn: 'Experiment analysis' },
  writing: { activeZh: '论文撰写中', activeEn: 'Drafting the paper', doneZh: '论文撰写', doneEn: 'Paper drafting' },
  proposal: { activeZh: '方案深耕中', activeEn: 'Refining the proposal', doneZh: '方案深耕', doneEn: 'Proposal refinement' },
  librarian: { activeZh: '精读编译中', activeEn: 'Reading & compiling papers', doneZh: '精读编译', doneEn: 'Reading & compiling' },
  present: { activeZh: '生成幻灯片中', activeEn: 'Building the slides', doneZh: '生成幻灯片', doneEn: 'Slide generation' },
};
const STAGE_FALLBACK = { activeZh: 'AI 处理中', activeEn: 'AI is working', doneZh: 'AI 处理', doneEn: 'AI processing' };
export function stageInfo(stage: string) {
  return STAGE_INFO[stage] ?? STAGE_FALLBACK;
}

export function hhmmss(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 一行结构化日志：时间戳 + 按 level 上色；step 行略微突出成分节。 */
export function LogLine({ entry }: { entry: LogEntry }) {
  const isStep = entry.level === 'step';
  return (
    <div
      className="row"
      style={{
        gap: 8,
        alignItems: 'flex-start',
        padding: isStep ? '5px 0 2px' : '1px 0',
        marginTop: isStep ? 5 : 0,
        borderTop: isStep ? '0.5px solid var(--terminal-border)' : 'none',
      }}
    >
      <span style={{ color: 'var(--terminal-dim)', flexShrink: 0, fontVariantNumeric: 'tabular-nums' }}>
        {hhmmss(entry.at)}
      </span>
      <span
        style={{
          color: LEVEL_COLOR[entry.level],
          fontWeight: isStep ? 650 : 400,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          minWidth: 0,
        }}
      >
        {entry.message}
      </span>
    </div>
  );
}

/** 大模型有时以 JSON 字符串形式吐代码/文本，换行是字面量 \n。终端按人类可读展示，
 *  把转义的空白还原成真实字符（本就是真实换行的内容里没有反斜杠转义，不受影响）。 */
export function normalizeLlmText(s: string): string {
  return s
    .replace(/\\r\\n/g, '\n')
    .replace(/\\n/g, '\n')
    .replace(/\\r/g, '\n')
    .replace(/\\t/g, '\t');
}

/** 已完成的大模型输出：定格为一条记录，长文本默认折叠只显示前几行。 */
export function LlmRecord({ entry }: { entry: LlmEntry }) {
  const [open, setOpen] = useState(false);
  const info = stageInfo(entry.stage);
  const text = normalizeLlmText(entry.text);
  const lines = text.split('\n');
  const long = lines.length > LLM_PREVIEW_LINES || text.length > 900;
  const shown = open || !long ? text : lines.slice(0, LLM_PREVIEW_LINES).join('\n');
  return (
    <div
      style={{
        margin: '6px 0',
        padding: '8px 10px',
        background: 'var(--terminal-bg-2)',
        border: '0.5px solid var(--terminal-border)',
        borderRadius: 8,
      }}
    >
      <div className="row" style={{ gap: 6 }}>
        <Icon name="sparkle" size={12} style={{ color: 'var(--terminal-accent)', flexShrink: 0 }} />
        <span style={{ color: 'var(--terminal-accent)', fontWeight: 650 }}>
          {tr(info.doneZh, info.doneEn)}
        </span>
        <span style={{ color: 'var(--terminal-dim)' }}>· {tr('输出完成', 'done')}</span>
        <span style={{ color: 'var(--terminal-dim)', marginLeft: 'auto', fontVariantNumeric: 'tabular-nums' }}>
          {hhmmss(entry.at)}
        </span>
      </div>
      {entry.text && (
        <div
          style={{
            marginTop: 5,
            color: 'var(--terminal-fg)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            opacity: 0.92,
          }}
        >
          {shown}
          {!open && long ? ' …' : ''}
        </div>
      )}
      {long && (
        <button
          onClick={() => setOpen((o) => !o)}
          style={{
            marginTop: 4,
            border: 'none',
            background: 'transparent',
            cursor: 'pointer',
            padding: 0,
            fontSize: 11,
            fontFamily: 'var(--mono)',
            color: 'var(--terminal-accent)',
          }}
        >
          {open ? tr('收起', 'Collapse') : tr('展开', 'Expand')}
        </button>
      )}
    </div>
  );
}

/** 正在输出的大模型活动块：打字机式实时增长 + 闪烁光标。 */
export function LlmActive({ active }: { active: ActiveLlm }) {
  const info = stageInfo(active.stage);
  return (
    <div
      style={{
        margin: '6px 0',
        padding: '8px 10px',
        background: 'var(--terminal-bg-2)',
        border: '0.5px solid var(--terminal-accent)',
        borderRadius: 8,
      }}
    >
      <div className="row" style={{ gap: 7 }}>
        <span className="dot pulse" style={{ background: 'var(--terminal-accent)', flexShrink: 0 }} />
        <span style={{ color: 'var(--terminal-accent)', fontWeight: 650 }}>
          {tr(info.activeZh, info.activeEn)} …
        </span>
      </div>
      {active.text && (
        <div style={{ marginTop: 5, color: 'var(--terminal-fg)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
          {normalizeLlmText(active.text)}
          <span style={{ color: 'var(--terminal-accent)', animation: 'ai-caret-blink 1s step-end infinite' }}>▋</span>
        </div>
      )}
    </div>
  );
}

interface RenderItem {
  key: string;
  at: string;
  node: ReactNode;
}

/** 终端面板：深色、等宽、自动滚到底；用户上滑看历史时暂停跟随并给「回到底部」。 */
export function TaskTerminal({
  state,
  live,
  onClear,
  height = 380,
  extraEntries,
  footer,
  headerExtras,
  filter,
}: {
  state: TerminalState;
  live: boolean;
  onClear: () => void;
  /** 终端滚动区高度（默认 380） */
  height?: number | string;
  /** 外部注入的块（AI 提问 / 用户消息），按 at 时间与日志条目归并 */
  extraEntries?: TerminalExtraEntry[];
  /** 面板底部插槽（如消息输入框） */
  footer?: ReactNode;
  /** 头部右侧插槽（如日志源切换 / 过滤开关） */
  headerExtras?: ReactNode;
  /** 条目过滤（只作用于日志/LLM 条目，不影响 extraEntries） */
  filter?: (entry: TerminalEntry) => boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const [showJump, setShowJump] = useState(false);
  // 默认只渲染最近 RENDER_WINDOW 条；用户点「显示更早」后渲染全部（能上滑翻完整历史）。
  const [showAllHistory, setShowAllHistory] = useState(false);

  // 跟随时：每次内容变化滚到底。
  useEffect(() => {
    if (!followRef.current) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [state, extraEntries]);

  // 清空后（或切任务重置）回到窗口模式，避免下次长任务一上来就渲染全部。
  useEffect(() => {
    if (state.entries.length === 0) setShowAllHistory(false);
  }, [state.entries.length]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    followRef.current = nearBottom;
    setShowJump((s) => (s === !nearBottom ? s : !nearBottom));
  };

  const jump = () => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    followRef.current = true;
    setShowJump(false);
  };

  // 日志/LLM 条目（可过滤）与外部注入块按 at 归并；两个来源各自已按时间有序。
  const baseEntries = filter ? state.entries.filter(filter) : state.entries;
  const items: RenderItem[] = baseEntries.map((e) => ({
    key: `${e.kind}-${e.id}`,
    at: e.at,
    node: e.kind === 'log' ? <LogLine entry={e} /> : <LlmRecord entry={e} />,
  }));
  let merged = items;
  if (extraEntries && extraEntries.length > 0) {
    const extras: RenderItem[] = extraEntries.map((x) => ({ key: `x-${x.id}`, at: x.at, node: x.node }));
    merged = [];
    let i = 0;
    let j = 0;
    // 同一时刻先日志后消息（偏序即可，不追求严格全序）
    while (i < items.length && j < extras.length) {
      const a = items[i]!;
      const b = extras[j]!;
      if (a.at <= b.at) {
        merged.push(a);
        i++;
      } else {
        merged.push(b);
        j++;
      }
    }
    merged.push(...items.slice(i), ...extras.slice(j));
  }

  const empty = merged.length === 0 && !state.active;

  return (
    <>
      <div className="row" style={{ margin: '20px 0 12px' }}>
        <span className="section-h">
          <Icon name="cpu" size={15} style={{ color: 'var(--accent)' }} />
          {tr('运行日志', 'Terminal')}
        </span>
        <div className="row gap8" style={{ marginLeft: 'auto' }}>
          {headerExtras}
          {live && (
            <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              <span className="dot pulse" />
              {tr('实时', 'live')}
            </span>
          )}
          <button
            className="btn btn-ghost sm"
            onClick={onClear}
            disabled={empty}
            title={tr('清空运行日志', 'Clear the terminal')}
          >
            <Icon name="trash" size={12} />
            {tr('清空', 'Clear')}
          </button>
        </div>
      </div>
      <div style={{ position: 'relative' }}>
        <div
          ref={scrollRef}
          onScroll={onScroll}
          style={{
            height,
            overflowY: 'auto',
            background: 'var(--terminal-bg)',
            border: '0.5px solid var(--terminal-border)',
            borderRadius: 12,
            padding: '12px 14px',
            fontFamily: 'var(--mono)',
            fontSize: 11.5,
            lineHeight: 1.65,
            color: 'var(--terminal-fg)',
          }}
        >
          {empty ? (
            <div style={{ color: 'var(--terminal-dim)', padding: '8px 2px' }}>
              {live
                ? tr('等待任务输出…', 'Waiting for task output…')
                : tr('暂无运行日志', 'No terminal output yet')}
            </div>
          ) : (
            <>
              {(() => {
                const hidden = showAllHistory ? 0 : Math.max(0, merged.length - RENDER_WINDOW);
                const visible = hidden > 0 ? merged.slice(hidden) : merged;
                return (
                  <>
                    {hidden > 0 && (
                      <button
                        onClick={() => setShowAllHistory(true)}
                        style={{
                          display: 'block',
                          width: '100%',
                          marginBottom: 6,
                          padding: '5px 0',
                          border: '0.5px dashed var(--terminal-border)',
                          borderRadius: 8,
                          background: 'transparent',
                          cursor: 'pointer',
                          fontSize: 11,
                          fontFamily: 'var(--mono)',
                          color: 'var(--terminal-accent)',
                        }}
                      >
                        {tr(`显示更早的 ${hidden} 条日志`, `Show ${hidden} earlier lines`)}
                      </button>
                    )}
                    {visible.map((item) => (
                      <Fragment key={item.key}>{item.node}</Fragment>
                    ))}
                  </>
                );
              })()}
              {state.active && <LlmActive active={state.active} />}
            </>
          )}
        </div>
        {showJump && (
          <button
            onClick={jump}
            className="row gap6"
            style={{
              position: 'absolute',
              bottom: 12,
              left: '50%',
              transform: 'translateX(-50%)',
              background: 'var(--terminal-bg-2)',
              color: 'var(--terminal-fg)',
              border: '0.5px solid var(--terminal-border)',
              borderRadius: 999,
              padding: '4px 12px',
              fontSize: 11,
              cursor: 'pointer',
              boxShadow: 'var(--shadow-pop)',
            }}
          >
            {tr('回到底部', 'Jump to bottom')}
            <Icon name="chevDown" size={12} />
          </button>
        )}
      </div>
      {footer}
    </>
  );
}
