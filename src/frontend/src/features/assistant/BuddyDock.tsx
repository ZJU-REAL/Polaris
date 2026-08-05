import { useCallback, useEffect, useRef, useState } from 'react';

/* ============================================================
   PolarisBuddy 的停靠栏：它是**版面的一列**，不是浮在页面上的抽屉。

   区别不只是观感：浮层盖住内容，于是「一边看论文一边问」在物理上做不到——用户得
   开一下关一下。停靠栏把内容挤窄，两边同时在场，这才是 Buddy 想要的用法。

   宽度可拖、记在 localStorage。拖动时才用 state 跟手，松手才落盘：每一像素写一次
   localStorage 会让拖拽变卡。

   窄屏不用这个（见 AppShell）：手机上挤两列谁都看不清，那里仍然是覆盖式抽屉。
   ============================================================ */

const WIDTH_KEY = 'polaris.buddy.width';
const MIN_WIDTH = 320;
//: 上限按视口算：固定 720 在小笔记本上会把内容区挤到没法看
const maxWidth = () => Math.max(MIN_WIDTH, Math.min(760, Math.round(window.innerWidth * 0.55)));

export const DEFAULT_WIDTH = 420;

export function clampWidth(width: number, viewportMax: number): number {
  return Math.min(Math.max(width, MIN_WIDTH), Math.max(MIN_WIDTH, viewportMax));
}

function loadWidth(): number {
  try {
    const raw = Number(localStorage.getItem(WIDTH_KEY));
    if (Number.isFinite(raw) && raw > 0) return clampWidth(raw, maxWidth());
  } catch {
    /* 存量坏值不值得报错，用默认宽度 */
  }
  return DEFAULT_WIDTH;
}

export function BuddyDock({ children }: { children: React.ReactNode }) {
  const [width, setWidth] = useState<number>(loadWidth);
  const [dragging, setDragging] = useState(false);
  const startRef = useRef<{ x: number; width: number } | null>(null);

  // 视口变窄时跟着收：否则把窗口拉小之后，停靠栏能把内容区挤没
  useEffect(() => {
    const onResize = () => setWidth((w) => clampWidth(w, maxWidth()));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      startRef.current = { x: e.clientX, width };
      setDragging(true);

      const onMove = (ev: PointerEvent) => {
        const start = startRef.current;
        if (!start) return;
        // 停靠在右侧：往左拖是变宽
        setWidth(clampWidth(start.width + (start.x - ev.clientX), maxWidth()));
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        startRef.current = null;
        setDragging(false);
        // 松手才落盘：拖动中每像素写一次 localStorage 会让拖拽发涩
        setWidth((w) => {
          try {
            localStorage.setItem(WIDTH_KEY, String(w));
          } catch {
            /* 存不下就下次重来，不是错误 */
          }
          return w;
        });
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    [width],
  );

  return (
    <div
      style={{
        flex: `0 0 ${width}px`,
        minWidth: 0,
        position: 'relative',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--surface)',
        borderLeft: '0.5px solid var(--border-2)',
      }}
    >
      {/* 拖拽把手：贴在左边缘外侧一点，命中区比看得见的线宽 */}
      <div
        onPointerDown={onPointerDown}
        title="拖动改变宽度"
        style={{
          position: 'absolute',
          left: -3,
          top: 0,
          bottom: 0,
          width: 7,
          cursor: 'col-resize',
          zIndex: 2,
          background: dragging ? 'var(--accent)' : 'transparent',
          opacity: dragging ? 0.35 : 1,
          touchAction: 'none',
        }}
      />
      {/* 拖动时全局禁选：否则光标扫过正文会把页面文字整片选中 */}
      {dragging && (
        <style>{`* { user-select: none !important; cursor: col-resize !important; }`}</style>
      )}
      {children}
    </div>
  );
}
