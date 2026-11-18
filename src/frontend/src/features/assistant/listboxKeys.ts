/* 下拉选单的键盘操作：上下键移动、Home/End 到头尾、Esc 关闭。
   只算「下一步落到哪」，焦点由调用方去挪。 */

export type ListboxKeyResult = { focus: number } | 'close' | null;

export function listboxKey(key: string, current: number, count: number): ListboxKeyResult {
  if (key === 'Escape') return 'close';
  if (count <= 0) return null;
  if (key === 'ArrowDown') return { focus: current < 0 ? 0 : (current + 1) % count };
  if (key === 'ArrowUp') return { focus: current < 0 ? count - 1 : (current - 1 + count) % count };
  if (key === 'Home') return { focus: 0 };
  if (key === 'End') return { focus: count - 1 };
  return null;
}
