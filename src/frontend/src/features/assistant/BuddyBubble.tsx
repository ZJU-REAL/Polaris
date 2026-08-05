import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { tr } from '../../lib/i18n';
import { PAPER_DND_MIME } from './paperDrag';

/* ============================================================
   PolarisBuddy 的悬浮球。

   - 可拖拽，位置记在 localStorage（下次进来还在老地方）；
   - 点击展开对话面板；拖动结束的那一下不算点击（按位移阈值区分）；
   - 是论文拖放的落点：论文卡片拖过来 → 高亮 → 松手把 paper_id 交给面板解读。
   ============================================================ */

const POS_KEY = 'polaris.buddy.pos';

//: 拖进来的文字截断长度。一整篇论文被拖进来时，问题本身不该是一篇论文。
const MAX_DROPPED_TEXT = 1200;
const CLICK_THRESHOLD_PX = 6;

interface Pos {
  right: number;
  bottom: number;
}

function loadPos(): Pos {
  try {
    const raw = localStorage.getItem(POS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Pos;
      if (typeof parsed.right === 'number' && typeof parsed.bottom === 'number') {
        return {
          right: Math.min(Math.max(parsed.right, 8), window.innerWidth - 64),
          bottom: Math.min(Math.max(parsed.bottom, 8), window.innerHeight - 64),
        };
      }
    }
  } catch {
    /* 坏数据回默认位 */
  }
  return { right: 24, bottom: 96 };
}

export function BuddyBubble({
  onOpen,
  onDropPaper,
  onDropText,
  nudge,
  onDismissNudge,
  busy,
}: {
  onOpen: () => void;
  /** 论文被拖到球上：paper_id 交给面板发起解读 */
  onDropPaper: (paperId: string) => void;
  /** 选中的文字被拖到球上：直接问这段话 */
  onDropText: (text: string) => void;
  /** 今天值得主动说的一句话；null = 不打扰 */
  nudge?: string | null;
  onDismissNudge?: () => void;
  busy: boolean;
}) {
  const [pos, setPos] = useState<Pos>(loadPos);
  const [dragOver, setDragOver] = useState(false);
  const dragState = useRef<{ startX: number; startY: number; moved: boolean } | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(POS_KEY, JSON.stringify(pos));
    } catch {
      /* 存不上就算了 */
    }
  }, [pos]);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      dragState.current = { startX: e.clientX, startY: e.clientY, moved: false };
      const origin = { ...pos };

      const onMove = (ev: PointerEvent) => {
        const dx = ev.clientX - (dragState.current?.startX ?? 0);
        const dy = ev.clientY - (dragState.current?.startY ?? 0);
        if (Math.abs(dx) + Math.abs(dy) > CLICK_THRESHOLD_PX && dragState.current) {
          dragState.current.moved = true;
        }
        setPos({
          right: Math.min(Math.max(origin.right - dx, 8), window.innerWidth - 64),
          bottom: Math.min(Math.max(origin.bottom - dy, 8), window.innerHeight - 64),
        });
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        // 没怎么动 = 点击
        if (dragState.current && !dragState.current.moved) onOpen();
        dragState.current = null;
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    [pos, onOpen],
  );

  return (
    <>
      {nudge && (
        <div
          onClick={onOpen}
          style={{
            position: 'fixed',
            right: pos.right + 60,
            bottom: pos.bottom + 4,
            maxWidth: 260,
            background: 'var(--surface)',
            border: '0.5px solid var(--border-2)',
            borderRadius: 12,
            boxShadow: '0 6px 20px rgba(0,0,0,0.12)',
            padding: '9px 11px',
            fontSize: 12.5,
            lineHeight: 1.6,
            color: 'var(--text-2)',
            cursor: 'pointer',
            zIndex: 69,
          }}
        >
          <div className="row gap6" style={{ alignItems: 'flex-start' }}>
            <span style={{ flex: 1, minWidth: 0 }}>{nudge}</span>
            <span
              className="hoverable"
              title={tr('今天不用再提醒我', 'Don’t remind me again today')}
              onClick={(e) => {
                e.stopPropagation();
                onDismissNudge?.();
              }}
              style={{ flexShrink: 0, color: 'var(--text-4)', cursor: 'pointer', lineHeight: 1 }}
            >
              <Icon name="x" size={11} />
            </span>
          </div>
        </div>
      )}
      <div
      role="button"
      aria-label="PolarisBuddy"
      title={tr(
        'PolarisBuddy（⌘J）· 可拖动 · 把论文或选中的文字拖过来',
        'PolarisBuddy (⌘J) · drag me · drop a paper or selected text on me',
      )}
      onPointerDown={onPointerDown}
      onDragOver={(e) => {
        // 平台内拖出的论文，或者页面上选中的一段文字
        const types = e.dataTransfer.types;
        if (types.includes(PAPER_DND_MIME) || types.includes('text/plain')) {
          e.preventDefault();
          setDragOver(true);
        }
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        // 论文优先：拖论文行时 text/plain 里是标题，不该被当成"解释这段话"
        const paperId = e.dataTransfer.getData(PAPER_DND_MIME);
        if (paperId) {
          onDropPaper(paperId);
          return;
        }
        const text = e.dataTransfer.getData('text/plain').trim();
        if (text) onDropText(text.slice(0, MAX_DROPPED_TEXT));
      }}
      style={{
        position: 'fixed',
        right: pos.right,
        bottom: pos.bottom,
        width: 52,
        height: 52,
        borderRadius: '50%',
        background: dragOver ? 'var(--accent)' : 'var(--surface)',
        border: `2px solid ${dragOver ? 'var(--accent)' : 'var(--accent-soft)'}`,
        boxShadow: '0 4px 16px rgba(0,0,0,0.14)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        cursor: 'grab',
        zIndex: 70,
        transform: dragOver ? 'scale(1.15)' : undefined,
        transition: 'transform 0.12s ease, background 0.12s ease',
        touchAction: 'none',
        userSelect: 'none',
      }}
    >
      <Icon
        name={busy ? 'refresh' : 'sparkle'}
        size={22}
        style={{
          color: dragOver ? '#fff' : 'var(--accent)',
          animation: busy ? 'spin 1.2s linear infinite' : undefined,
        }}
      />
      </div>
    </>
  );
}
