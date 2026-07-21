import { useEffect, useState } from 'react';
import { Icon, type IconName } from './Icon';

/* ============================================================
   轻量 toast（自制，无第三方依赖）。
   模块级队列 + <ToastHost/>（挂在 AppShell），任意处调用 toast()。
   ============================================================ */

export type ToastKind = 'info' | 'ok' | 'error';

interface ToastItem {
  id: number;
  message: string;
  kind: ToastKind;
}

let items: ToastItem[] = [];
let listener: ((items: ToastItem[]) => void) | null = null;
let seq = 0;

const TTL_MS = 4200;

/** 弹出一条 toast，自动消失。 */
export function toast(message: string, kind: ToastKind = 'info'): void {
  const item: ToastItem = { id: ++seq, message, kind };
  items = [...items.slice(-4), item];
  listener?.(items);
  setTimeout(() => {
    items = items.filter((i) => i.id !== item.id);
    listener?.(items);
  }, TTL_MS);
}

const KIND_ICON: Record<ToastKind, IconName> = { info: 'bell', ok: 'check', error: 'x' };
const KIND_COLOR: Record<ToastKind, string> = {
  info: 'var(--accent-soft-2)',
  ok: 'var(--ok)',
  error: 'var(--danger)',
};

/** 挂载一次即可（AppShell）。 */
export function ToastHost() {
  const [list, setList] = useState<ToastItem[]>(items);
  useEffect(() => {
    listener = setList;
    setList(items);
    return () => {
      if (listener === setList) listener = null;
    };
  }, []);
  if (list.length === 0) return null;
  return (
    <div className="toast-host">
      {list.map((t) => (
        <div key={t.id} className="toast" style={{ borderLeftColor: KIND_COLOR[t.kind] }}>
          <Icon name={KIND_ICON[t.kind]} size={14} style={{ flexShrink: 0, opacity: 0.85 }} />
          <span>{t.message}</span>
        </div>
      ))}
    </div>
  );
}
