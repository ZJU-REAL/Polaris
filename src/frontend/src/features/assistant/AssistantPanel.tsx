import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { SpeechPlayer } from '../../components/ui/SpeechPlayer';
import { PolarisMark } from '../../components/ui/PolarisLogo';
import { Markdown } from '../../lib/markdown';
import { api } from '../../lib/api';
import {
  assistantTurnSse,
  type AssistantBlock,
  type ImageRef,
  type PaperSource,
  type PlanStep,
} from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';
import { copyText } from '../../lib/clipboard';
import { BuddyHome } from './BuddyHome';
import { followUps } from './followups';
import { InlineFigure } from './InlineFigure';
import { ConversationRail } from './ConversationRail';
import { ToolImages } from './ToolImages';
import { pageContextFrom } from './buddyContext';
import { useProject } from '../../app/project';
import { TurnStatus } from './TurnStatus';

/* ============================================================
   PolarisBuddy：全局抽屉。⌘J 开关，或点右下角的悬浮球。

   与现有六个对话入口并存——它们一行没改。Buddy 能跨课题/库/论文调用平台工具，
   界面上多出来的就是「工具卡片」：正在调什么、调完拿到了什么。

   渲染必须**防御式**：SSE 过来的是 JSON.parse 出来的 any，TypeScript 只在编译期
   帮你。未知事件忽略、未知块画成占位，绝不抛——这个项目没有前端测试工具，
   运行时炸了没人拦得住。
   ============================================================ */

interface Turn {
  role: 'user' | 'assistant';
  blocks: AssistantBlock[];
}

/** 服务端持久化的块 → 前端块。认不出的形状一律降级，绝不抛。

    工具结果里存的是**摘要**（summary/preview/图片引用），不是完整 payload——回放要的
    是「调了什么、拿到了什么样的东西」，而完整结果动辄几万字符。

    ``cardById`` 由调用方传进来、**跨消息共用**：一轮会落成两条消息——assistant 那条
    带 ``tool_use``，紧跟的 ``tool_results`` 那条带 ``tool_result``。这张表以前建在函数
    里、每条消息重来一次，于是回放 ``tool_results`` 时压根找不到上一条的卡片，结果
    回填不上，两件事跟着出错：卡片永远没有 summary，标签就一直显示「调用中…」（图标
    却是 ✓）；而这条消息一个块都没产出，触发下面的兜底，把
    ``[工具结果] {"summary": ...}`` 整段原始 JSON 当正文打进对话里。 */
export function restoreBlocks(
  m: { text: string; blocks: unknown[]; kind?: string },
  cardById: Map<string, Extract<AssistantBlock, { kind: 'tool' }>>,
): AssistantBlock[] {
  const out: AssistantBlock[] = [];

  for (const raw of Array.isArray(m.blocks) ? m.blocks : []) {
    if (!raw || typeof raw !== 'object') continue;
    const b = raw as Record<string, unknown>;

    if (b.kind === 'text' && typeof b.text === 'string') {
      const last = out[out.length - 1];
      // 落库时正文是逐段写的，回放要拼回一整块，否则每个 token 都成了独立段落
      if (last && last.kind === 'text') last.text += b.text;
      else out.push({ kind: 'text', text: b.text });
    } else if (b.kind === 'thinking' && typeof b.text === 'string') {
      out.push({ kind: 'thinking', text: b.text });
    } else if (b.kind === 'tool_use') {
      const card: Extract<AssistantBlock, { kind: 'tool' }> = {
        kind: 'tool',
        id: String(b.id ?? ''),
        name: String(b.name ?? '?'),
        // 结果还没读到之前先当成功：历史里的调用都已经结束了，画成 running 会骗人
        state: 'ok',
      };
      cardById.set(card.id, card);
      out.push(card);
    } else if (b.kind === 'tool_result') {
      // 结果回填到对应卡片上（含图片引用）——切走再回来，工具过程与图片都还在
      // 跨消息也找不到对应卡片，说明这条结果是孤儿（存量数据或落库出错）。
      // 跳过就好：宁可少一张卡，也不要把几万字符的原始结果倒进对话。
      const card = cardById.get(String(b.tool_use_id ?? ''));
      if (!card) continue;
      let payload: Record<string, unknown> = {};
      try {
        const parsed: unknown = JSON.parse(String(b.content ?? '{}'));
        if (parsed && typeof parsed === 'object') payload = parsed as Record<string, unknown>;
      } catch {
        /* 存量数据里 content 是原始结果文本，不是摘要 JSON：忽略即可 */
      }
      if (typeof payload.summary === 'string') card.summary = payload.summary;
      if (typeof payload.preview === 'string') card.preview = payload.preview;
      if (typeof payload.duration_ms === 'number') card.durationMs = payload.duration_ms;
      if (b.is_error === true || payload.ok === false) card.state = 'error';
      const refs = parseRestoredImageRefs(payload.image_refs);
      if (refs.length) card.images = refs;
    }
  }
  // 兜底只对**真的有话要说**的消息生效。tool_results 那条的 text 是把工具结果拍平成
  // 的 `[工具结果] {...}`（见 core/llm/base.py _block_text），那是给日志和 token 估算
  // 看的，不是给人看的——它没块可还原时，正确的做法是什么都不显示。
  if (out.length === 0 && m.text && m.kind !== 'tool_results') {
    out.push({ kind: 'text', text: m.text });
  }
  return out;
}

/** 落库的图片引用 → 前端形状；认不出的丢掉。 */
function parseRestoredImageRefs(raw: unknown): ImageRef[] {
  if (!Array.isArray(raw)) return [];
  const out: ImageRef[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const ref = item as Record<string, unknown>;
    if (ref.kind !== 'paper_figure') continue;
    const paperId = typeof ref.paper_id === 'string' ? ref.paper_id : '';
    const index = typeof ref.index === 'number' ? ref.index : undefined;
    if (!paperId || index === undefined) continue;
    out.push({
      kind: 'paper_figure',
      paperId,
      index,
      label: typeof ref.label === 'string' ? ref.label : undefined,
    });
  }
  return out;
}

/** 后端错误码 → 用户看得懂的一句话。认不出的原样透出，别把线索吃掉。 */
function errorText(detail: string): string {
  if (detail === 'CHAT_AGENT_DISABLED') {
    return tr('助手在这个部署上没开启，找管理员开一下。', 'The assistant is switched off on this deployment — ask an admin to enable it.');
  }
  if (detail === 'LLM_NOT_CONFIGURED') {
    return tr('还没配可用的模型，去设置里配一个。', 'No usable model is configured yet — set one up in settings.');
  }
  if (detail === 'QUOTA_EXCEEDED') {
    return tr('你的 AI 用量已经用完了。', 'You have used up your AI quota.');
  }
  return detail;
}

function ToolCard({ block }: { block: Extract<AssistantBlock, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false);
  const color =
    block.state === 'error' ? 'var(--danger)' : block.state === 'ok' ? 'var(--ok-tx)' : 'var(--text-4)';
  return (
    <div
      style={{
        border: '0.5px solid var(--border-2)',
        borderRadius: 8,
        background: 'var(--surface-2)',
        padding: '7px 10px',
        margin: '6px 0',
        fontSize: 12,
      }}
    >
      <div
        className="row gap8 hoverable"
        style={{ cursor: 'pointer', alignItems: 'center' }}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon
          name={block.state === 'running' ? 'refresh' : block.state === 'error' ? 'x' : 'check'}
          size={12}
          style={{
            color,
            animation: block.state === 'running' ? 'spin 1s linear infinite' : undefined,
          }}
        />
        <span className="mono" style={{ color: 'var(--text-2)' }}>{block.name}</span>
        <span style={{ color: 'var(--text-3)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {block.summary ?? tr('调用中…', 'running…')}
        </span>
        {block.durationMs !== undefined && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
            {block.durationMs}ms
          </span>
        )}
      </div>
      {block.images?.length ? <ToolImages images={block.images} /> : null}
      {open && block.preview && (
        <pre
          className="mono"
          style={{
            marginTop: 6,
            maxHeight: 220,
            overflow: 'auto',
            fontSize: 11,
            color: 'var(--text-3)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
          }}
        >
          {block.preview}
        </pre>
      )}
    </div>
  );
}

/** 会动的标识：思考/作答中只画蓝色主体并轻微呼吸，答完把右上角那个绿点补上。

    用标识自己的一笔当状态，而不是再摆一个转圈图标——转圈是通用的「在忙」，
    而「还差一点」正好说明它在把这件事做完。呼吸幅度压得很小：状态提示要能被余光
    看见，但不该在读答案时抢注意力。 */
export function BuddyMark({ busy, size = 16 }: { busy: boolean; size?: number }) {
  return (
    <span
      style={{
        display: 'inline-flex',
        animation: busy ? 'buddy-breathe 1.6s ease-in-out infinite' : undefined,
      }}
    >
      <PolarisMark size={size} dot={!busy} />
    </span>
  );
}

/** 任务计划：做到哪一步了。步骤状态是模型自己更新的，界面只是如实画出来。 */
function PlanCard({
  steps,
  awaitingApproval,
  onApprove,
  onRevise,
}: {
  steps: PlanStep[];
  /** 计划模式交上来等点头的那份；推进中的进度条不带这个 */
  awaitingApproval?: boolean;
  onApprove?: () => void;
  onRevise?: () => void;
}) {
  const done = steps.filter((s) => s.status === 'done').length;
  return (
    <div
      style={{
        border: '0.5px solid var(--border-2)',
        borderRadius: 9,
        background: 'var(--surface-2)',
        padding: '9px 11px',
        margin: '8px 0',
      }}
    >
      <div
        className="row gap6"
        style={{ alignItems: 'center', marginBottom: 6, fontSize: 11.5, color: 'var(--text-3)' }}
      >
        <Icon name="layers" size={12} style={{ color: 'var(--accent)' }} />
        <span>{awaitingApproval ? tr('这样做行吗？', 'Shall I go ahead?') : tr('计划', 'Plan')}</span>
        {/* 待审批时不报「0/N」：一步都还没做，进度数字只会让人以为它已经开工了 */}
        {!awaitingApproval && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
            {done}/{steps.length}
          </span>
        )}
      </div>
      {steps.map((step, i) => (
        <div
          key={`${i}-${step.title}`}
          className="row gap6"
          style={{
            alignItems: 'flex-start',
            fontSize: 12.5,
            lineHeight: 1.6,
            color: step.status === 'done' ? 'var(--text-4)' : 'var(--text-2)',
          }}
        >
          <Icon
            name={step.status === 'done' ? 'check' : step.status === 'running' ? 'refresh' : 'dot'}
            size={11}
            style={{
              marginTop: 4,
              flexShrink: 0,
              color:
                step.status === 'done'
                  ? 'var(--ok-tx)'
                  : step.status === 'running'
                    ? 'var(--accent)'
                    : 'var(--text-4)',
              animation: step.status === 'running' ? 'spin 1.1s linear infinite' : undefined,
            }}
          />
          <span
            style={{
              textDecoration: step.status === 'done' ? 'line-through' : undefined,
              fontWeight: step.status === 'running' ? 600 : 400,
            }}
          >
            {step.title}
          </span>
        </div>
      ))}
      {/* 批准是**一次点击**的事。让用户再打一行「可以」有两个毛病：多打一行字，
          以及那句「可以」漂在正文里，模型未必知道它在批准哪份计划。 */}
      {awaitingApproval && (onApprove || onRevise) && (
        <div className="row gap6" style={{ marginTop: 10 }}>
          {onApprove && (
            <button className="btn btn-primary sm" onClick={onApprove}>
              {tr('按这个做', 'Go ahead')}
            </button>
          )}
          {onRevise && (
            <button className="btn btn-ghost sm" onClick={onRevise}>
              {tr('改一改', 'Revise it')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** 思考块：跑的时候只露最后一行（瞟一眼就够），停了折起来。 */
function ThinkingView({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(false);
  const tail = text.trim().split('\n').filter(Boolean).slice(-1)[0] ?? '';
  return (
    <div style={{ margin: '4px 0' }}>
      <div
        className="row gap6 hoverable"
        style={{ cursor: 'pointer', alignItems: 'center', fontSize: 11.5, color: 'var(--text-4)' }}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon
          name="chevDown"
          size={11}
          style={{ transform: open ? undefined : 'rotate(-90deg)', transition: 'transform 0.12s' }}
        />
        <span>{tr('思考过程', 'Thinking')}</span>
        {!open && live && (
          <span
            style={{
              flex: 1,
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              opacity: 0.75,
            }}
          >
            {tail}
          </span>
        )}
      </div>
      {open && (
        <div
          style={{
            fontSize: 12,
            color: 'var(--text-3)',
            whiteSpace: 'pre-wrap',
            borderLeft: '2px solid var(--border-2)',
            paddingLeft: 9,
            marginTop: 4,
            lineHeight: 1.6,
          }}
        >
          {text}
        </div>
      )}
    </div>
  );
}

/** 这一轮它看过的论文。**不叫「引用」**：模型写的 [n] 指的是谁，我们无从核对，
    叫引用就是在替它担保。点进去是论文本身——回答里提到某篇却点不进去，是这套界面
    最容易让人卡住的地方。 */
function SourcesCard({ papers }: { papers: PaperSource[] }) {
  return (
    <div style={{ margin: '10px 0 2px' }}>
      <div style={{ fontSize: 11, color: 'var(--text-4)', marginBottom: 4 }}>
        {tr(`本轮查看的论文 · ${papers.length}`, `Papers looked at · ${papers.length}`)}
      </div>
      <div className="col gap4">
        {papers.map((paper, i) => (
          <a
            key={paper.paperId}
            href={`/papers/${paper.paperId}/read`}
            className="row gap6 hoverable"
            style={{
              alignItems: 'flex-start',
              fontSize: 12,
              lineHeight: 1.5,
              color: 'var(--text-2)',
              textDecoration: 'none',
              padding: '3px 5px',
              borderRadius: 6,
            }}
          >
            <span className="mono" style={{ color: 'var(--text-4)', flexShrink: 0 }}>
              {i + 1}.
            </span>
            <span style={{ minWidth: 0 }}>{paper.title}</span>
          </a>
        ))}
      </div>
    </div>
  );
}

/** 验收提请注意。判据是启发式的，所以措辞要让人一眼看穿并能忽略——
    装成权威结论比不提示更糟。 */
function VerifyCard({ notes }: { notes: string[] }) {
  return (
    <div
      style={{
        margin: '8px 0 2px',
        padding: '7px 10px',
        borderRadius: 8,
        background: 'var(--warn-bg)',
        color: 'var(--warn-tx)',
        fontSize: 11.5,
        lineHeight: 1.6,
      }}
    >
      <div style={{ marginBottom: 2 }}>{tr('这段回答有地方对不上：', 'Something in this answer does not line up:')}</div>
      {notes.map((note) => (
        <div key={note}>· {note}</div>
      ))}
    </div>
  );
}

function BlockView({
  block,
  live,
  sources,
  onApprovePlan,
  onRevisePlan,
}: {
  block: AssistantBlock;
  live: boolean;
  sources: PaperSource[];
  onApprovePlan?: () => void;
  onRevisePlan?: () => void;
}) {
  if (block.kind === 'text') {
    // 模型会在正文里直接写 ![图注](paper_id/图号) 和 [1] 这类引用。Markdown 组件本来
    // 就有 renderLibraryFigure / renderCitation 两个钩子（文献对话一直在用），Buddy
    // 只是一个都没传——于是图不显示、引用点不动。
    return (
      <Markdown
        source={block.text}
        renderLibraryFigure={(paperId, index) => <InlineFigure paperId={paperId} index={index} />}
        renderCitation={(n) => {
          const src = sources[n - 1];
          if (!src) return null;
          return (
            <a
              href={`/papers/${src.paperId}/read`}
              title={src.title}
              style={{
                display: 'inline-block',
                padding: '0 4px',
                margin: '0 1px',
                borderRadius: 5,
                background: 'var(--accent-soft)',
                color: 'var(--accent-text)',
                fontSize: '0.82em',
                fontWeight: 650,
                textDecoration: 'none',
              }}
            >
              {n}
            </a>
          );
        }}
      />
    );
  }
  if (block.kind === 'plan')
    return (
      <PlanCard
        steps={block.steps}
        awaitingApproval={block.awaitingApproval}
        onApprove={onApprovePlan}
        onRevise={onRevisePlan}
      />
    );
  if (block.kind === 'sources') return <SourcesCard papers={block.papers} />;
  if (block.kind === 'verify') return <VerifyCard notes={block.notes} />;
  if (block.kind === 'thinking') return <ThinkingView text={block.text} live={live} />;
  if (block.kind === 'tool') return <ToolCard block={block} />;
  return null; // 未知块：画不出来就不画，绝不抛
}

export function AssistantPanel({
  open,
  onClose,
  droppedPaperId,
  onDroppedPaperHandled,
  droppedText,
  onDroppedTextHandled,
  onBusyChange,
  variant = 'overlay',
}: {
  open: boolean;
  onClose: () => void;
  /** dock = 版面的一列（宽度与边框由 BuddyDock 负责）；overlay = 窄屏覆盖式抽屉 */
  variant?: 'dock' | 'overlay';
  /** 拖到悬浮球上的论文 id：进来就自动问一句「解读这篇」 */
  droppedPaperId?: string | null;
  onDroppedPaperHandled?: () => void;
  /** 拖到悬浮球上的一段文字：直接问这段 */
  droppedText?: string | null;
  onDroppedTextHandled?: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  // 「哪些会话在跑」是**一张按 id 的表**，不是一个布尔。
  //
  // 以前是单个 busy + 单个 abortRef，整个面板共用：A 在跑时切到 B，B 也显示停止
  // 按钮，点下去掐掉的是 A 那条流；而切走之后 A 在列表里的呼吸灯立刻灭了，尽管
  // 它还在跑。对话本来就该并行，各自的状态就得各自存。
  const [running, setRunning] = useState<Set<string>>(() => new Set());
  //: 还没落库的新会话（convId 还是 null）也可能在跑，用这个占位
  const NEW_CONV = '__new__';
  const [convId, setConvId] = useState<string | null>(null);
  // dock 形态下会话列表是常驻一栏（不再是头部时钟弹层）；overlay 形态屏幕太窄，
  // 仍然用弹层。
  //
  // **默认收起**：打开 Buddy 是为了问一句话，不是为了翻旧账。两栏一起铺开会把本来
  // 就窄的对话区再切一刀，而历史一天用不上一次。手动开过就记住，别每次都要再点。
  const [railOpen, setRailOpen] = useState<boolean>(() => {
    try {
      return localStorage.getItem('polaris.buddyRailOpen') === '1';
    } catch {
      return false;
    }
  });
  const toggleRail = useCallback(() => {
    setRailOpen((o) => {
      const next = !o;
      try {
        localStorage.setItem('polaris.buddyRailOpen', next ? '1' : '0');
      } catch {
        /* 隐私模式：仅本次会话生效 */
      }
      return next;
    });
  }, []);
  const [historyOpen, setHistoryOpen] = useState(false);
  //: 发完一轮之后标题才生成，列表要跟着刷新一次
  const [railRefresh, setRailRefresh] = useState(0);
  const [conversations, setConversations] = useState<
    { id: string; title: string; project_id: string | null }[]
  >([]);
  // 问候语改成前端按本地时间算（见 greeting.ts），这里只要名字。
  // queryKey 与 AppShell 一致，命中同一份缓存，不多发一次请求。
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  // 开场的问句与三条候选：后端按「他此刻在看什么」+ 他自己的近况挑，不过模型。
  const [opening, setOpening] = useState<
    { question: string; cards: { kind: string; summary: string; prompt: string }[] } | null
  >(null);
  const [model, setModel] = useState<string>('');
  const [title, setTitle] = useState<string>('');
  // 页面上下文照常发给模型，但不在输入框上占一行——它是背景信息，不是待办事项。
  const contextOn = true;
  //: 会话 id → 掐掉它那条 SSE 的函数。停止只停这一条，不动别人。
  const abortById = useRef<Map<string, () => void>>(new Map());
  const scrollRef = useRef<HTMLDivElement>(null);
  const location = useLocation();
  // 对话里的「项目」就是平台里的课题。**默认不选**：选了之后检索范围就收窄到这个
  // 课题。**默认跟随你正在做的那个课题**：人问「实验跑得怎么样」时，指的多半就是
  // 眼前这个课题的实验，而不是全实验室的。想问别的课题、或者想放开到全部资产，
  // 点上面那个按钮改——改了就以你改的为准，不再被界面切换带着走。
  //
  // 以前这里是个开关，只能绑当前课题、不能挑；再往前默认是「全部」，于是问一句
  // 「我的实验」会把别的课题的也捞进来。
  const { projects, currentProjectId } = useProject();
  const [topicId, setTopicId] = useState<string | null>(currentProjectId);
  // 用户自己动过选择之后，就别再跟着界面切课题——那是他的选择，不是界面的。
  const topicPinned = useRef(false);
  useEffect(() => {
    if (!topicPinned.current) setTopicId(currentProjectId);
  }, [currentProjectId]);
  const pickTopic = useCallback((id: string | null) => {
    topicPinned.current = true;
    setTopicId(id);
  }, []);
  const [topicOpen, setTopicOpen] = useState(false);
  const [topicQuery, setTopicQuery] = useState('');
  const topic = useMemo(
    () => projects.find((p) => p.id === topicId) ?? null,
    [projects, topicId],
  );
  const topicMatches = useMemo(() => {
    const q = topicQuery.trim().toLowerCase();
    return q ? projects.filter((p) => p.name.toLowerCase().includes(q)) : projects;
  }, [projects, topicQuery]);
  // chat = 默认（行为一个字没变）；plan = 只调研出计划等人点头；goal = 带着一个持续目标
  const [mode, setMode] = useState<'chat' | 'plan' | 'goal'>('chat');
  // 目标模式的目标**取自你说的第一句话**，不再单开一行让人填。
  // 多出来的那一行有两个毛病：切到目标模式时版面突然长高一截，而且它问的
  // 「这场对话要达成什么」和下面输入框要打的字，多数时候本来就是同一句。
  const [goal, setGoal] = useState('');
  const [plusOpen, setPlusOpen] = useState(false);
  const pageContext = pageContextFrom(location.pathname);
  //: 「**当前这场**在跑吗」——界面上多数地方问的是这个
  const busy = running.has(convId ?? NEW_CONV);

  // 只有「本来就贴着底」才跟着滚。正在往回翻的时候被拽回最新一行，是流式界面里最
  // 烦人的一种行为——用户已经用滚动明确表达了「我在看别的地方」。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTo({ top: el.scrollHeight });
  }, [turns]);

  // 面板卸载时把所有还在跑的都掐掉，别留下野连接
  useEffect(() => () => abortById.current.forEach((fn) => fn()), []);

  useEffect(() => {
    onBusyChange?.(busy);
  }, [busy, onBusyChange]);

  // 开面板时取一次问候语。数字是 SQL 数出来的，**不过模型**——每次开面板过一次 LLM
  // 既慢又费钱，而且模型会把数字说错。取不到就不说，不要兜一句假的（0 和「没数出来」
  // 在界面上长得一样，对用户却是两回事）。

  // 开面板时按「此刻在看什么」取一次开场。换了页面就重取——在读论文时问的和在
  // 实验页问的本来就该不一样；这条请求不过模型，快且不花钱。
  useEffect(() => {
    if (!open) return;
    let alive = true;
    void api
      .getBuddyGreeting(pageContext?.kind ?? null)
      .then((g) => {
        if (alive) setOpening({ question: g.question, cards: g.cards ?? [] });
      })
      .catch(() => {
        // 取不到就不摆开场——宁可只有一句问候，也不要挂着一句假的
        if (alive) setOpening(null);
      });
    return () => {
      alive = false;
    };
  }, [open, pageContext?.kind]);

  /** 这一场跑完了：从在跑集合里摘掉，并丢掉它的中止函数。 */
  const finish = useCallback((id: string) => {
    abortById.current.delete(id);
    setRunning((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  /** 停掉某一场对话。不传就停当前这场。

      以前这里停的是「面板唯一那条流」，于是在 B 里点停止会掐掉 A。 */
  const stopConv = useCallback((id: string | null) => {
    const key = id ?? NEW_CONV;
    // 掐掉 SSE 连接。后端会把已生成的部分落库标成 interrupted，重开会话还能看到。
    abortById.current.get(key)?.();
    abortById.current.delete(key);
    setRunning((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  }, []);

  const openHistory = useCallback(async () => {
    setHistoryOpen((o) => !o);
    try {
      setConversations(await api.listAssistantConversations());
    } catch {
      setConversations([]);
    }
  }, []);

  const loadConversation = useCallback(
    async (id: string) => {
      // 服务端才是历史的权威源：本地状态直接整体替换
      try {
        const messages = await api.getAssistantMessages(id);
        setConvId(id);
        setHistoryOpen(false);
        // 会话上存着的课题才是这场对话真正的作用域，跟着切过去。
        // （这句注释以前是空头支票：写着「跟着切」，却没有一行代码在切。）
        // 会话自己的作用域也算「定下来了」：它是这场对话当初问的范围，
        // 不该被用户后来在界面上切课题给盖掉。
        pickTopic(conversations.find((c) => c.id === id)?.project_id ?? null);
        const cardById = new Map<string, Extract<AssistantBlock, { kind: 'tool' }>>();
        setTurns(
          messages
            // 一轮会落成几条消息：assistant（正文/调用）+ 携带工具结果的那条。
            // 后者 role 是 user（Anthropic 形状），但**不是用户说的话**——按 kind 认出来
            // 并入上一轮，否则历史里会冒出一堆空的「提问」。
            // 卡片表在**整轮之外**建：tool_use 与 tool_result 分属两条消息，
            // 每条消息各建一张表就永远对不上（见 restoreBlocks 的注释）。
            .reduce<Turn[]>((turns, m) => {
              if (m.role !== 'user' && m.role !== 'assistant') return turns;
              const blocks = restoreBlocks(m, cardById);
              const last = turns[turns.length - 1];
              if (m.kind === 'tool_results' && last) {
                last.blocks = [...last.blocks, ...blocks];
                return turns;
              }
              turns.push({ role: m.role as 'user' | 'assistant', blocks });
              return turns;
            }, []),
        );
      } catch {
        /* 载入失败保持现状 */
      }
    },
    [conversations, pickTopic],
  );

  const newConversation = useCallback(() => {
    stopConv(convId);
    // 新的一场对话不该背着上一场的目标
    setGoal('');
    setConvId(null);
    setTurns([]);
    setTitle('');
    setHistoryOpen(false);
  }, [stopConv, convId]);

  const ask = useCallback(
    async (question: string) => {
    if (!question || busy) return;
    // 目标模式：还没有目标就把这一句立为目标，并记下来给后续每一轮用
    const activeGoal = mode === 'goal' ? goal.trim() || question : '';
    if (mode === 'goal' && !goal.trim()) setGoal(question);
    // 起跑：先按当前 id 记上（新会话用占位，拿到真 id 后改名）。
    // 「在跑」是**每场对话各自**的状态，不再是一个全局 busy，所以这里记的是 id 而不是 true
    const startedAt = convId ?? NEW_CONV;
    setRunning((prev) => new Set(prev).add(startedAt));
    setTurns((t) => [...t, { role: 'user', blocks: [{ kind: 'text', text: question }] }, { role: 'assistant', blocks: [] }]);

    let id = convId;
    try {
      if (!id) {
        id = (await api.createAssistantConversation({})).id;
        setConvId(id);
        // 占位换成真 id：否则这场跑起来了，列表里那条却不亮
        setRunning((prev) => {
          const next = new Set(prev);
          next.delete(NEW_CONV);
          next.add(id!);
          return next;
        });
      }
    } catch (e) {
      // 这里以前把所有失败都说成「助手未启用」，把真正的原因吃掉了
      const detail = e instanceof Error ? e.message : String(e);
      setTurns((t) => {
        const next = [...t];
        next[next.length - 1] = { role: 'assistant', blocks: [{ kind: 'text', text: `⚠️ ${errorText(detail)}` }] };
        return next;
      });
      finish(startedAt);
      return;
    }

    const patch = (fn: (blocks: AssistantBlock[]) => AssistantBlock[]) =>
      setTurns((t) => {
        const next = [...t];
        const last = next[next.length - 1];
        if (!last || last.role !== 'assistant') return t;
        next[next.length - 1] = { ...last, blocks: fn(last.blocks) };
        return next;
      });

    const turnId = id;
    abortById.current.set(turnId, () => undefined); // 占位，真函数在下面赋值
    const abort = assistantTurnSse(
      id,
      question,
      {
        // 页面上下文是**线索不是授权**：它只说用户在看什么，真要读内容还得调工具，
        // 那条路上的权限校验一点没少。用户可以关掉。
        page:
          contextOn && pageContext ? { kind: pageContext.kind, id: pageContext.id } : undefined,
        projectId: topicId,
        mode,
        // 第一句话立为目标，之后每轮都带同一个——不然第二轮会把新问题当成新目标，
        // 「一直朝一个目标推进」就变成了「每轮换一个目标」。
        goal: mode === 'goal' ? activeGoal : undefined,
        onMeta: (meta) => setModel(meta.model),
        onBlocks: patch,
        onDone: () => {
          finish(turnId);
          setRailRefresh((n) => n + 1);
          // 标题是这一轮结束后才生成的，回头取一次
          void api
            .listAssistantConversations()
            .then((rows) => setTitle(rows.find((r) => r.id === id)?.title ?? ''))
            .catch(() => undefined);
        },
        onError: (detail) => {
          // 把原始细节一并留下：只写「网络错误」等于让用户和我们都无从查起
          patch((blocks) => [
            ...blocks,
            { kind: 'text', text: `⚠️ ${errorText(detail)}\n\n\`${detail}\`` },
          ]);
          finish(turnId);
        },
      },
    );
    abortById.current.set(turnId, abort);
    },
    [busy, convId, contextOn, pageContext, topicId, mode, goal],
  );

  const send = useCallback(() => {
    const question = input.trim();
    if (!question) return;
    setInput('');
    void ask(question);
  }, [input, ask]);

  /** 重问某个回答对应的问题：从 ``at`` 往回找最近的**用户**轮。

      以前这里写的是 ``turns[i - 1]``，假定问题就在回答的前一格。新发起的一轮确实
      如此，但回放历史之后不是：``loadConversation`` 会把 tool_results 并进上一轮，
      用户轮与助手轮之间未必相邻。取不到就得到空串，``if (text)`` 不成立，于是这个
      按钮**点了什么都不发生**——最难查的那种坏，因为它看起来完好。 */
  const retryFrom = useCallback(
    (at: number) => {
      for (let i = at - 1; i >= 0; i -= 1) {
        const turn = turns[i];
        if (turn?.role !== 'user') continue;
        const text = turn.blocks
          .map((b) => (b.kind === 'text' ? b.text : ''))
          .join('')
          .trim();
        if (text) return ask(text);
        break; // 找到了用户轮却没有正文：那就没什么可重问的
      }
      return undefined;
    },
    [turns, ask],
  );

  /** 批准计划：切回一般模式，让它按刚才那份计划动手。

      切回一般模式是关键——留在计划模式里，模型下一轮又会去"调研并提交计划"，
      用户点了「按这个做」却看见它再交一份计划，会以为按钮没生效。 */
  const approvePlan = useCallback(() => {
    setMode('chat');
    void ask(
      tr(
        '按你刚才那份计划做，边做边用 update_plan 更新进度。',
        'Go ahead with the plan you just proposed; keep it updated with update_plan as you go.',
      ),
    );
  }, [ask]);

  /** 让它改：留在计划模式，把话头交回给用户说哪儿不对。 */
  const revisePlan = useCallback(() => {
    // 留在计划模式，把话头交回给用户：他说哪儿不对，模型据此重排一份。
    // 这里只清空输入框，不替他打字——「哪儿不对」只有他知道。
    setInput('');
  }, []);

  // 论文被拖到悬浮球上：直接问一句解读。id 写进问题里，Buddy 自己去 get_paper。
  useEffect(() => {
    if (!droppedPaperId || busy) return;
    onDroppedPaperHandled?.();
    void ask(
      tr(
        `帮我解读这篇论文（id: ${droppedPaperId}）：它解决什么问题、方法怎么工作、证据强不强。`,
        `Walk me through this paper (id: ${droppedPaperId}): the problem, how the method works, how strong the evidence is.`,
      ),
    );
  }, [droppedPaperId, busy, ask, onDroppedPaperHandled]);

  // 选中的文字被拖过来：把原文原样带上，让 Buddy 就着这段说
  useEffect(() => {
    if (!droppedText || busy) return;
    onDroppedTextHandled?.();
    void ask(
      tr(
        `解释一下这段话，必要时去平台里查证：\n\n「${droppedText}」`,
        `Explain this passage, checking the platform if needed:\n\n“${droppedText}”`,
      ),
    );
  }, [droppedText, busy, ask, onDroppedTextHandled]);

  if (!open) return null;

  // dock 形态下外框（宽度/边框/拖拽把手）归 BuddyDock 管，这里只铺内容；
  // overlay 形态自己就是那个浮层。两种形态共用同一套正文，不分叉。
  const frame: React.CSSProperties =
    variant === 'dock'
      ? { flex: 1, minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }
      : {
          position: 'fixed',
          top: 0,
          right: 0,
          bottom: 0,
          width: 'min(520px, 100vw)',
          background: 'var(--surface)',
          borderLeft: '0.5px solid var(--border-2)',
          display: 'flex',
          flexDirection: 'column',
          zIndex: 60,
          boxShadow: '-8px 0 24px rgba(0,0,0,0.06)',
        };

  const lastTurn = turns[turns.length - 1];
  const liveBlocks = busy && lastTurn?.role === 'assistant' ? lastTurn.blocks : [];

  return (
    <div style={frame}>
      {/* 高度与主面板顶栏（.topbar, 52px）严格一致：两栏并排时分割线必须在同一水平线上，
          差几个像素比差很多更显眼——眼睛会把它读成「没对齐」而不是「另一种设计」。 */}
      <div
        className="row gap8"
        style={{
          height: 52,
          flexShrink: 0,
          padding: '0 16px',
          borderBottom: '0.5px solid var(--border)',
          // 与 .topbar 同一底色：两栏并排时颜色差一点点，看起来像没加载完
          background: 'rgba(247, 249, 252, 0.85)',
          backdropFilter: 'blur(8px)',
          alignItems: 'center',
        }}
      >
        <BuddyMark busy={busy} size={17} />
        <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', lineHeight: 1.25 }}>
          <div className="row gap6" style={{ alignItems: 'center', minWidth: 0 }}>
            {/* 运行中的呼吸灯：标题栏与会话列表用同一个记号，扫一眼就知道哪场还在跑 */}
            {busy && <span className="buddy-live-dot" />}
            <strong
              style={{ fontSize: 13.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
              title={title || 'PolarisBuddy'}
            >
              {title || 'PolarisBuddy'}
            </strong>
          </div>
          {model && (
            <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>
              {model}
            </span>
          )}
        </div>
        <span style={{ flex: 1 }} />
        {variant === 'dock' ? (
          <button
            className="icon-btn"
            onClick={toggleRail}
            title={tr('会话列表', 'Conversations')}
            style={{ color: railOpen ? 'var(--accent)' : undefined }}
          >
            <Icon name="sidebar" size={14} />
          </button>
        ) : (
          <button
            className="icon-btn"
            onClick={() => void openHistory()}
            title={tr('历史会话', 'Conversations')}
          >
            <Icon name="clock" size={14} />
          </button>
        )}
        <button className="icon-btn" onClick={newConversation} title={tr('新会话', 'New')}>
          <Icon name="plus" size={14} />
        </button>
        <button
          className="icon-btn"
          onClick={onClose}
          title={
            variant === 'dock'
              ? tr('收起（⌘J）', 'Collapse (⌘J)')
              : tr('关闭（⌘J）', 'Close (⌘J)')
          }
        >
          <Icon name={variant === 'dock' ? 'chevron' : 'x'} size={14} />
        </button>
      </div>


      {historyOpen && (
        <div
          style={{
            borderBottom: '0.5px solid var(--border-2)',
            maxHeight: 220,
            overflowY: 'auto',
            padding: '6px 8px',
          }}
        >
          {conversations.length === 0 && (
            <div style={{ fontSize: 12, color: 'var(--text-4)', padding: 8 }}>
              {tr('还没有历史会话', 'No conversations yet')}
            </div>
          )}
          {conversations.map((c) => (
            <div
              key={c.id}
              className="hoverable"
              onClick={() => void loadConversation(c.id)}
              style={{
                padding: '7px 9px',
                borderRadius: 7,
                fontSize: 12.5,
                cursor: 'pointer',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                background: c.id === convId ? 'var(--accent-soft)' : undefined,
              }}
            >
              {c.title || tr('（未命名）', '(untitled)')}
            </div>
          ))}
        </div>
      )}

      <div className="row" style={{ flex: 1, minHeight: 0, minWidth: 0, alignItems: 'stretch' }}>
      {variant === 'dock' && railOpen && (
        <ConversationRail
          activeId={convId}
          onPick={(id) => void loadConversation(id)}
          onNew={newConversation}
          refreshKey={railRefresh}
          // 列表里**所有**在跑的都亮，不只是当前这场——切走之后那条还在跑，
          // 灯就该一直亮到它结束。
          runningIds={running}
        />
      )}
      {/* overflowWrap: anywhere 是给长 URL、DOI、无空格的模型名留的后路——它们不换行，
          会把整个对话框顶宽，而对话框是 flex 子项，顶宽的结果是把页面撑破。 */}
      <div
        ref={scrollRef}
        className="buddy-scroll"
        style={{
          flex: 1,
          minWidth: 0,
          overflowY: 'auto',
          overflowX: 'hidden',
          overflowWrap: 'anywhere',
          padding: '14px 16px',
        }}
      >
        {turns.length === 0 && (
          <BuddyHome
            name={me?.display_name}
            question={opening?.question}
            cards={opening?.cards}
            onPick={(prompt) => setInput(prompt)}
          />
        )}
        {turns.map((turn, i) => {
          const isLast = i === turns.length - 1;
          return (
            <div key={i} style={{ marginBottom: 14 }}>
              {turn.role === 'user' ? (
                <div
                  style={{
                    background: 'var(--accent-soft)',
                    color: 'var(--accent-text)',
                    borderRadius: 9,
                    padding: '8px 11px',
                    fontSize: 13,
                    overflowWrap: 'anywhere',
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {turn.blocks.map((b, j) => (b.kind === 'text' ? <span key={j}>{b.text}</span> : null))}
                </div>
              ) : (
                <div style={{ fontSize: 13, lineHeight: 1.7 }}>
                  {turn.blocks.map((b, j) => (
                    <BlockView
                      key={j}
                      block={b}
                      live={busy && isLast && j === turn.blocks.length - 1}
                      sources={
                        (turn.blocks.find((x) => x.kind === 'sources') as
                          | { kind: 'sources'; papers: PaperSource[] }
                          | undefined)?.papers ?? []
                      }
                      // 只有最后一轮的计划还能批：翻上去看历史时那些按钮点了也没意义
                      onApprovePlan={isLast && !busy ? approvePlan : undefined}
                      onRevisePlan={isLast && !busy ? revisePlan : undefined}
                    />
                  ))}
                  {isLast && (
                    <TurnStatus blocks={liveBlocks} busy={busy} onStop={() => stopConv(convId)} />
                  )}
                  {/* 答完之后的操作行：复制、重来。**不放点赞/点踩**——没有接收端的
                      反馈按钮是摆设，点了之后什么都没发生比没有按钮更伤信任。 */}
                  {!busy && turn.blocks.some((b) => b.kind === 'text') && (
                    <div className="row gap4" style={{ marginTop: 8, alignItems: 'center' }}>
                      <SpeechPlayer
                        text={turn.blocks
                          .filter((b) => b.kind === 'text')
                          .map((b) => (b.kind === 'text' ? b.text : ''))
                          .join('\n')}
                      />
                      <button
                        className="icon-btn"
                        title={tr('复制回答', 'Copy answer')}
                        onClick={() => {
                          const text = turn.blocks
                            .filter((b) => b.kind === 'text')
                            .map((b) => (b.kind === 'text' ? b.text : ''))
                            .join('\n');
                          void copyText(text);
                        }}
                        style={{ width: 24, height: 24 }}
                      >
                        <Icon name="file" size={12} />
                      </button>
                      {isLast && (
                        <button
                          className="icon-btn"
                          title={tr('换一个回答', 'Answer again')}
                          onClick={() => void retryFrom(i)}
                          style={{ width: 24, height: 24 }}
                        >
                          <Icon name="refresh" size={12} />
                        </button>
                      )}
                    </div>
                  )}
                  {/* 答完给几条可点的下一步。它们从本轮真实发生的事里出——查到了论文
                      就问共同点，取过图就问图说明什么。凭空造的建议点一次就没人再点。 */}
                  {isLast && !busy && followUps(turn.blocks).length > 0 && (
                    <div className="row gap6 wrap" style={{ marginTop: 10 }}>
                      {followUps(turn.blocks).map((q) => (
                        <span
                          key={q}
                          className="chip"
                          style={{ cursor: 'pointer' }}
                          onClick={() => void ask(q)}
                        >
                          {q}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
      </div>

      <div style={{ padding: 12, borderTop: '0.5px solid var(--border-2)' }}>
        {/* 上下文栏贴在输入框顶上（Codex 那样）：课题与模式是这次提问的前置条件，
            放进框里会和正文抢注意力，放到别处又和输入脱节。 */}
        <div
          className="row gap8"
          style={{
            alignItems: 'center',
            padding: '6px 10px',
            border: '0.5px solid var(--border-2)',
            borderBottom: 'none',
            borderRadius: '12px 12px 0 0',
            background: 'var(--surface-2)',
            fontSize: 11.5,
            color: 'var(--text-3)',
            position: 'relative',
          }}
        >
          <button
            className="icon-btn"
            onClick={() => setPlusOpen((o) => !o)}
            title={tr('课题与模式', 'Topic and mode')}
            style={{ width: 22, height: 22 }}
          >
            <Icon name="plus" size={13} />
          </button>

          {/* 课题选择：和加号并排。以前这里是一行说明文字，只告诉你现在查的是什么，
              要换得去别处切；现在它自己就是入口——点开挑一个，再点「所有资产」放开。

              这里选的是**这次问话够得着哪些资产**：选了课题，就只看这个课题名下的
              东西（关联文献库、实验、想法、在写的稿子）；不选就是你能看到的全部。
              实验室级的资产（每日新论文、公共文献库）两种情况下都在——它们不属于
              任何一个课题，收窄课题不该把它们也关掉。 */}
          <button
            className="buddy-scope"
            onClick={() => {
              setTopicQuery('');
              setTopicOpen((o) => !o);
            }}
            aria-haspopup="listbox"
            aria-expanded={topicOpen}
            title={
              topic
                ? `${tr('只用这个课题名下的资产', 'Only this topic’s assets')} · ${topic.name}`
                : tr(
                    '你能看到的所有资产；选一个课题可收窄到它名下',
                    'Everything you can see; pick a topic to narrow to it',
                  )
            }
            data-picked={topic ? '1' : undefined}
          >
            <Icon name="layers" size={12} />
            <span className="buddy-scope-label">
              {topic ? topic.name : tr('所有资产', 'All assets')}
            </span>
            <Icon name="chevDown" size={11} />
          </button>

          {mode !== 'chat' && (
            <>
              <span style={{ color: 'var(--border-2)' }}>·</span>
              <span
                className="hoverable"
                style={{ cursor: 'pointer', color: 'var(--accent-text)' }}
                onClick={() => { setMode('chat'); setGoal(''); }}
                title={tr('点掉回到普通对话', 'Click to return to plain chat')}
              >
                {mode === 'plan' ? tr('计划模式', 'Plan mode') : tr('目标模式', 'Goal mode')}
              </span>
            </>
          )}

          <span style={{ flex: 1 }} />
          {model && (
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }} title={model}>
              {model}
            </span>
          )}

          {topicOpen && (
            <>
              <div onClick={() => setTopicOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 49 }} />
              <div className="buddy-scope-menu" role="listbox">
                {/* 课题可能有几十个，光靠滚的找不到——列表一长就先给个搜索框 */}
                {projects.length > 7 && (
                  <input
                    className="input sm"
                    autoFocus
                    value={topicQuery}
                    onChange={(e) => setTopicQuery(e.target.value)}
                    placeholder={tr('搜课题…', 'Search topics…')}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') setTopicOpen(false);
                      // 搜到只剩一个时回车直接选它
                      const only = topicMatches.length === 1 ? topicMatches[0] : undefined;
                      if (e.key === 'Enter' && only) {
                        pickTopic(only.id);
                        setTopicOpen(false);
                      }
                    }}
                    style={{ marginBottom: 4 }}
                  />
                )}
                <div className="buddy-scope-list">
                  <button
                    className="buddy-scope-item"
                    role="option"
                    aria-selected={!topicId}
                    onClick={() => {
                      pickTopic(null);
                      setTopicOpen(false);
                    }}
                  >
                    <Icon name="grid" size={13} />
                    <span className="buddy-scope-item-label">{tr('所有资产', 'All assets')}</span>
                    {!topicId && <Icon name="check" size={12} />}
                  </button>
                  {projects.length > 0 && <div className="hr" style={{ margin: '4px 2px' }} />}
                  {topicMatches.map((p) => (
                    <button
                      key={p.id}
                      className="buddy-scope-item"
                      role="option"
                      aria-selected={p.id === topicId}
                      title={p.name}
                      onClick={() => {
                        pickTopic(p.id);
                        setTopicOpen(false);
                      }}
                    >
                      <Icon name="layers" size={13} />
                      <span className="buddy-scope-item-label">{p.name}</span>
                      {p.id === topicId && <Icon name="check" size={12} />}
                    </button>
                  ))}
                  {/* 一个课题都没有和「搜不到」是两回事，分开说 */}
                  {topicMatches.length === 0 && (
                    <div className="buddy-scope-empty">
                      {projects.length === 0
                        ? tr('还没有可见的课题', 'No topics you can see yet')
                        : tr('没有匹配的课题', 'No topic matches that')}
                    </div>
                  )}
                </div>
              </div>
            </>
          )}

          {plusOpen && (
            <>
              <div onClick={() => setPlusOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 49 }} />
              <div
                className="col gap4"
                style={{
                  position: 'absolute',
                  left: 6,
                  bottom: 34,
                  zIndex: 50,
                  minWidth: 244,
                  padding: 6,
                  background: 'var(--surface)',
                  border: '0.5px solid var(--border-2)',
                  borderRadius: 12,
                  boxShadow: '0 10px 28px rgba(0,0,0,0.14)',
                }}
                onClick={() => setPlusOpen(false)}
              >
                {/* 三种模式并列、单选。以前只列 plan 与 goal，回到普通对话得再点一次
                    已经亮着的那个——「取消勾选」这个动作没人猜得到，等于普通模式没有入口。 */}
                <button className="btn btn-ghost sm" style={{ justifyContent: 'flex-start' }} onClick={() => { setMode('chat'); setGoal(''); }}>
                  <Icon name="chat" size={13} />
                  <span style={{ flex: 1, textAlign: 'left' }}>{tr('一般模式 · 直接回答', 'Normal · just answer')}</span>
                  {mode === 'chat' && <Icon name="check" size={12} />}
                </button>
                <button className="btn btn-ghost sm" style={{ justifyContent: 'flex-start' }} onClick={() => { setMode('plan'); setGoal(''); }}>
                  <Icon name="bulb" size={13} />
                  <span style={{ flex: 1, textAlign: 'left' }}>{tr('计划模式 · 先出方案，你点头再做', 'Plan · propose first, act on approval')}</span>
                  {mode === 'plan' && <Icon name="check" size={12} />}
                </button>
                <button className="btn btn-ghost sm" style={{ justifyContent: 'flex-start' }} onClick={() => setMode('goal')}>
                  <Icon name="compass" size={13} />
                  <span style={{ flex: 1, textAlign: 'left' }}>{tr('目标模式 · 一直朝一个目标推进', 'Goal mode · keep pursuing one goal')}</span>
                  {mode === 'goal' && <Icon name="check" size={12} />}
                </button>
              </div>
            </>
          )}
        </div>



        <div
          style={{
            border: '0.5px solid var(--border-2)',
            borderRadius: '0 0 12px 12px',
            background: 'var(--surface)',
            padding: '10px 12px 8px',
          }}
        >
          <textarea
            className="textarea"
            rows={1}
            value={input}
            placeholder={tr('今天想让我做点什么？', 'How can I help you today?')}
            onChange={(e) => {
              setInput(e.target.value);
              const el = e.target;
              el.style.height = 'auto';
              el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            style={{
              width: '100%',
              fontSize: 13.5,
              border: 'none',
              outline: 'none',
              resize: 'none',
              background: 'transparent',
              padding: 0,
              maxHeight: 140,
              lineHeight: '21px',
            }}
          />
          <div className="row gap8" style={{ alignItems: 'center', marginTop: 6 }}>
            <span style={{ flex: 1, fontSize: 10.5, color: 'var(--text-4)' }}>
              {tr('Enter 发送 · Shift+Enter 换行', 'Enter to send · Shift+Enter for a new line')}
            </span>
            {busy ? (
              <button
                onClick={stop}
                title={tr('停止', 'Stop')}
                style={{
                  width: 28, height: 28, borderRadius: '50%', border: 'none',
                  background: 'var(--surface-3)', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}
              >
                <Icon name="pause" size={12} />
              </button>
            ) : (
              <button
                onClick={send}
                disabled={!input.trim()}
                title={tr('发送', 'Send')}
                style={{
                  width: 28, height: 28, borderRadius: '50%', border: 'none',
                  background: input.trim() ? 'var(--accent)' : 'var(--surface-3)',
                  color: input.trim() ? '#fff' : 'var(--text-4)',
                  cursor: input.trim() ? 'pointer' : 'default',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}
              >
                <Icon name="arrow" size={13} />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
