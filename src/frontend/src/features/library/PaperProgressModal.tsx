import { useEffect, useRef, useState } from 'react';
import { Modal } from '../../components/ui/Modal';
import { Icon } from '../../components/ui/Icon';
import { subscribeSse } from '../../lib/sse';
import { tr } from '../../lib/i18n';

/* ============================================================
   手动添加文献后的分阶段处理进度弹窗。
   订阅 GET /paper-tasks/{taskId}/events（SSE）：
   - event=stage → 更新对应阶段状态（best-effort：单阶段 error 不代表整体失败）
   - event=done  → 收尾并短暂延迟后自动关闭 + 回调 onDone（刷新列表）
   - event=error → 顶层致命错误，保留弹窗与关闭按钮，不自动关
   阶段固定顺序：download → extract → embed → score → summarize（解析元数据在同步请求
   已完成，不单列）。summarize = 给加进文献库的论文生成解读。
   ============================================================ */

type StageId = 'download' | 'extract' | 'embed' | 'score' | 'summarize';
type StageStatus = 'pending' | 'running' | 'ok' | 'skipped' | 'error';

// 模块级常量保留 zh/en，渲染处再 tr（import 时求值不随语言切换更新）
const STAGES: { id: StageId; zh: string; en: string }[] = [
  { id: 'download', zh: '下载 PDF', en: 'Downloading PDF' },
  { id: 'extract', zh: '提取正文', en: 'Extracting text' },
  { id: 'embed', zh: '建立索引', en: 'Indexing' },
  { id: 'score', zh: '评估相关度', en: 'Scoring relevance' },
  { id: 'summarize', zh: '生成解读', en: 'Generating summary' },
];

/** 「生成解读」这一步的说明：引擎发来的是内部短语，换成一句用户能看懂的话。
    返回 null = 不显示说明（「已跳过」三个字已经够了）。 */
export function summarizeDetail(status: StageStatus, detail: string | undefined): string | null {
  if (status === 'running') return tr('需要一两分钟', 'This takes a minute or two');
  if (status === 'error') {
    return tr('没有生成成功，可以稍后在论文页重新生成', 'Couldn’t generate it. Try again from the paper page.');
  }
  if (status !== 'skipped') return null;
  switch (detail) {
    case 'already summarized':
      return tr('这篇论文已有解读', 'This paper already has a summary');
    case 'batch too large':
      return tr('一次添加超过 20 篇时不自动生成，可以在论文页生成', 'Not generated for more than 20 papers at once. Generate it from the paper page.');
    case 'no full text or abstract':
      return tr('没有全文和摘要，无法生成', 'No full text or abstract to summarise');
    default:
      return null;
  }
}

/** 弹窗标题跟着状态走：以前处理完了还写着「正在处理论文」，旁边却是一个对勾。 */
export function progressTitle(state: 'running' | 'done' | 'partial' | 'failed'): string {
  if (state === 'failed') return tr('论文处理失败', 'Couldn’t process the paper');
  // 整体走完了，但有步骤没成功：不能配一个对勾说「已处理完成」
  if (state === 'partial') return tr('论文已添加，部分步骤没有成功', 'Paper added, but some steps didn’t succeed');
  if (state === 'done') return tr('论文已处理完成', 'Paper processed');
  return tr('正在处理论文', 'Processing paper');
}

interface StageState {
  status: StageStatus;
  detail?: string;
}

/** 服务端 stage 事件里合法的终态/进行态（不含前端内部的 pending）。 */
type WireStageStatus = 'running' | 'ok' | 'skipped' | 'error';

function initStages(): Record<StageId, StageState> {
  return STAGES.reduce(
    (acc, s) => {
      acc[s.id] = { status: 'pending' };
      return acc;
    },
    {} as Record<StageId, StageState>,
  );
}

/** 单阶段左侧的状态图标。 */
function StageIcon({ status }: { status: StageStatus }) {
  if (status === 'running') {
    return <Icon name="refresh" size={14} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />;
  }
  if (status === 'ok') {
    return <Icon name="check" size={15} style={{ color: 'var(--ok)' }} />;
  }
  if (status === 'skipped') {
    return <Icon name="minus" size={15} style={{ color: 'var(--text-3)' }} />;
  }
  if (status === 'error') {
    // 图标集里没有 warning，内联一个与 Icon 同风格（stroke / currentColor）的三角警示
    return (
      <svg
        width={15}
        height={15}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ color: 'var(--warn)' }}
      >
        <path d="M12 3 2.5 20h19z" />
        <path d="M12 10v4M12 17.3h.01" />
      </svg>
    );
  }
  // pending：弱化的灰点
  return (
    <span
      style={{
        width: 8,
        height: 8,
        borderRadius: '50%',
        background: 'var(--muted-dot)',
        display: 'inline-block',
        margin: '0 3.5px',
      }}
    />
  );
}

export interface PaperProgressModalProps {
  taskId: string;
  paperTitle?: string;
  onClose: () => void;
  /** 处理完整（done）后回调，通常用于 invalidate 列表 query。 */
  onDone?: () => void;
}

export function PaperProgressModal({ taskId, paperTitle, onClose, onDone }: PaperProgressModalProps) {
  const [stages, setStages] = useState<Record<StageId, StageState>>(initStages);
  const [done, setDone] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);

  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    let stopped = false;
    let closeTimer: ReturnType<typeof setTimeout> | undefined;

    const cancel = subscribeSse(`/paper-tasks/${taskId}/events`, {
      onEvent: (event, data) => {
        if (stopped) return;
        if (event === 'stage') {
          try {
            const d = JSON.parse(data) as { stage: StageId; status: WireStageStatus; detail?: string };
            setStages((prev) => (prev[d.stage] ? { ...prev, [d.stage]: { status: d.status, detail: d.detail } } : prev));
          } catch {
            /* 忽略解析失败的片段 */
          }
        } else if (event === 'done') {
          // 仍在 running 的阶段收尾为 ok；pending 的维持原样
          setStages((prev) => {
            const next = { ...prev };
            for (const s of STAGES) {
              if (next[s.id].status === 'running') next[s.id] = { status: 'ok' };
            }
            return next;
          });
          setDone(true);
          stopped = true;
          cancel(); // 正常结束，主动断开避免 subscribeSse 自动重连
          closeTimer = setTimeout(() => {
            onDoneRef.current?.();
            onCloseRef.current();
          }, 1100);
        } else if (event === 'error') {
          let message = tr('处理失败', 'Processing failed');
          try {
            const m = (JSON.parse(data) as { message?: string }).message;
            if (m) message = m;
          } catch {
            /* keep default */
          }
          setFatal(message);
          stopped = true;
          cancel();
        }
      },
    });

    return () => {
      stopped = true;
      cancel();
      if (closeTimer) clearTimeout(closeTimer);
    };
  }, [taskId]);

  const stageFailed = STAGES.some((s) => stages[s.id].status === 'error');
  const sub = paperTitle ?? (done ? tr('处理完成', 'Done') : tr('处理中…', 'Processing…'));

  return (
    <Modal
      open
      onClose={onClose}
      width={460}
      title={
        <>
          <Icon
            name={fatal ? 'x' : done ? (stageFailed ? 'minus' : 'check') : 'sparkle'}
            size={15}
            style={{ color: fatal || (done && stageFailed) ? 'var(--warn)' : done ? 'var(--ok)' : 'var(--accent)' }}
          />
          {progressTitle(fatal ? 'failed' : done ? (stageFailed ? 'partial' : 'done') : 'running')}
        </>
      }
      sub={sub}
      footer={
        <button className="btn btn-ghost sm" onClick={onClose}>
          {done ? tr('完成', 'Done') : tr('关闭', 'Close')}
        </button>
      }
    >
      <div className="col" style={{ gap: 2 }}>
        {STAGES.map((s) => {
          const st = stages[s.id];
          const dim = st.status === 'pending' || st.status === 'skipped';
          // 跳过的原因是引擎的内部短语（"no target library" 之类），「已跳过」已经够了；
          // 出错时保留引擎给的原因。生成解读这一步有自己的说明。
          const detail =
            s.id === 'summarize'
              ? summarizeDetail(st.status, st.detail)
              : st.status === 'skipped'
                ? null
                : st.detail;
          return (
            <div key={s.id} className="row gap10" style={{ padding: '7px 2px', alignItems: 'flex-start' }}>
              <span
                className="row"
                style={{ width: 18, height: 20, justifyContent: 'center', flexShrink: 0 }}
              >
                <StageIcon status={st.status} />
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div
                  className="row gap6"
                  style={{ fontSize: 13, fontWeight: 500, color: dim ? 'var(--text-3)' : 'var(--text)', lineHeight: 1.4 }}
                >
                  {tr(s.zh, s.en)}
                  {st.status === 'skipped' && (
                    <span style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 400 }}>
                      {tr('已跳过', 'Skipped')}
                    </span>
                  )}
                </div>
                {detail && (
                  <div
                    style={{
                      fontSize: 11,
                      lineHeight: 1.5,
                      marginTop: 2,
                      color: st.status === 'error' ? 'var(--warn-tx)' : 'var(--text-3)',
                    }}
                  >
                    {detail}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {fatal && (
        <div
          style={{
            marginTop: 14,
            fontSize: 12,
            lineHeight: 1.6,
            color: 'var(--danger-tx)',
            background: 'var(--danger-bg)',
            borderRadius: 8,
            padding: '9px 11px',
          }}
        >
          {fatal}
        </div>
      )}
      {done && !fatal && (
        <div
          style={{
            marginTop: 14,
            fontSize: 12,
            lineHeight: 1.6,
            color: stageFailed ? 'var(--warn-tx)' : 'var(--ok-tx)',
            background: stageFailed ? 'var(--warn-bg)' : 'var(--ok-bg)',
            borderRadius: 8,
            padding: '9px 11px',
          }}
        >
          {stageFailed
            ? tr('没有成功的步骤，原因见上方', 'See above for why a step didn’t succeed.')
            : tr('处理完成', 'Done')}
        </div>
      )}
    </Modal>
  );
}
