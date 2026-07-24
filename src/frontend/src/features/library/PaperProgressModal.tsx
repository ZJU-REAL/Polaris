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
   阶段固定顺序：download → extract → embed → score（解析元数据在同步请求已完成，不单列）。
   ============================================================ */

type StageId = 'download' | 'extract' | 'embed' | 'score';
type StageStatus = 'pending' | 'running' | 'ok' | 'skipped' | 'error';

// 模块级常量保留 zh/en，渲染处再 tr（import 时求值不随语言切换更新）
const STAGES: { id: StageId; zh: string; en: string }[] = [
  { id: 'download', zh: '下载 PDF', en: 'Downloading PDF' },
  { id: 'extract', zh: '抽取正文', en: 'Extracting text' },
  { id: 'embed', zh: '向量化', en: 'Embedding' },
  { id: 'score', zh: '打分', en: 'Scoring' },
];

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

  const sub = paperTitle ?? (done ? tr('处理完成', 'Done') : tr('正在后台处理，请稍候…', 'Working in the background…'));

  return (
    <Modal
      open
      onClose={onClose}
      width={460}
      title={
        <>
          <Icon
            name={fatal ? 'x' : done ? 'check' : 'sparkle'}
            size={15}
            style={{ color: fatal ? 'var(--warn)' : done ? 'var(--ok)' : 'var(--accent)' }}
          />
          {tr('正在处理文献', 'Processing paper')}
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
                  style={{ fontSize: 12.5, fontWeight: 560, color: dim ? 'var(--text-3)' : 'var(--text)', lineHeight: 1.4 }}
                >
                  {tr(s.zh, s.en)}
                  {st.status === 'skipped' && (
                    <span style={{ fontSize: 10.5, color: 'var(--text-3)', fontWeight: 400 }}>
                      {tr('已跳过', 'Skipped')}
                    </span>
                  )}
                </div>
                {st.detail && (
                  <div
                    style={{
                      fontSize: 10.5,
                      lineHeight: 1.5,
                      marginTop: 2,
                      color: st.status === 'error' ? 'var(--warn-tx)' : 'var(--text-3)',
                    }}
                  >
                    {st.detail}
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
            fontSize: 11.5,
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
            fontSize: 11.5,
            lineHeight: 1.6,
            color: 'var(--ok-tx)',
            background: 'var(--ok-bg)',
            borderRadius: 8,
            padding: '9px 11px',
          }}
        >
          {tr('文献已处理完成。', 'Paper processed.')}
        </div>
      )}
    </Modal>
  );
}
