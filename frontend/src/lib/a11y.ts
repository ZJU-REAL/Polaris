import type { KeyboardEvent } from 'react';

/**
 * 可点击的非 button 元素（卡片、chip 等）的键盘可达性属性：
 * role/tabIndex + Enter/Space 触发，展开到 JSX 上即可。
 */
export function clickable(onActivate: () => void) {
  return {
    role: 'button' as const,
    tabIndex: 0,
    onClick: onActivate,
    onKeyDown: (e: KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onActivate();
      }
    },
  };
}
