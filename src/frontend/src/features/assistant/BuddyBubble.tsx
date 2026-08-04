import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { tr } from '../../lib/i18n';

/* ============================================================
   PolarisBuddy 的悬浮球。

   - 可拖拽，位置记在 localStorage（下次进来还在老地方）；
   - 点击展开对话面板；拖动结束的那一下不算点击（按位移阈值区分）；
   - 是论文拖放的落点：论文卡片拖过来 → 高亮 → 松手把 paper_id 交给面板解读。
   ============================================================ */

const POS_KEY = 'polaris.buddy.pos';
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
  busy,
}: {
  onOpen: () => void;
  /** 论文被拖到球上：paper_id 交给面板发起解读 */
  onDropPaper: (paperId: string) => void;
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
    <div
      role="button"
      aria-label="PolarisBuddy"
      title={tr('PolarisBuddy（⌘J）· 可拖动 · 把论文拖过来让我解读', 'PolarisBuddy (⌘J) · drag me · drop a paper on me')}
      onPointerDown={onPointerDown}
      onDragOver={(e) => {
        // 只认平台内拖出的论文
        if (e.dataTransfer.types.includes('application/x-polaris-paper')) {
          e.preventDefault();
          setDragOver(true);
        }
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        const paperId = e.dataTransfer.getData('application/x-polaris-paper');
        if (paperId) onDropPaper(paperId);
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
  );
}
