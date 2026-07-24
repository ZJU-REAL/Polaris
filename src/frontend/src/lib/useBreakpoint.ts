import { useSyncExternalStore } from 'react';

/* ============================================================
   响应式断点（全站唯一口径）。
   - 断点值在三处出现，必须同步：本文件的 BP、styles/tokens.css 的
     --bp-* 变量、styles/global.css 里 @media 的字面量。
     （CSS 变量不能用在 @media 条件里，所以媒体查询只能写字面量。）
   - 只有内联 style 吃不到媒体查询的地方才用这里的 hook；能用 CSS
     解决的一律写进 global.css，别在组件里手写 window.innerWidth。
   ============================================================ */

export const BP = {
  /** 平板 / 小笔记本：双栏改上下堆叠、页面留白收窄 */
  compact: 1024,
  /** 手机：关掉全局 zoom、侧栏改覆盖抽屉、顶栏收窄 */
  mobile: 768,
} as const;

type Key = keyof typeof BP;

interface Store {
  subscribe: (fn: () => void) => () => void;
  getSnapshot: () => boolean;
}

/** 每个断点一个 store：subscribe / getSnapshot 必须是稳定引用，
    否则 useSyncExternalStore 每次渲染都会退订重订。 */
function makeStore(key: Key): Store {
  const supported = typeof window !== 'undefined' && typeof window.matchMedia === 'function';
  if (!supported) {
    return { subscribe: () => () => {}, getSnapshot: () => false };
  }
  const list = window.matchMedia(`(max-width: ${BP[key]}px)`);
  // 缓存快照：getSnapshot 必须返回稳定值，不能每次现读 list.matches
  // 之外再包一层新对象；布尔值本身可直接比较，读 list 也安全，但缓存
  // 能保证订阅回调触发前后读到一致的值。
  let snapshot = list.matches;
  return {
    subscribe: (fn) => {
      const onChange = () => {
        snapshot = list.matches;
        fn();
      };
      list.addEventListener('change', onChange);
      return () => list.removeEventListener('change', onChange);
    },
    getSnapshot: () => snapshot,
  };
}

const stores: Record<Key, Store> = {
  compact: makeStore('compact'),
  mobile: makeStore('mobile'),
};

/** 服务端 / 无 matchMedia 环境按桌面渲染。 */
function getServerSnapshot(): boolean {
  return false;
}

function useBreakpoint(key: Key): boolean {
  const store = stores[key];
  return useSyncExternalStore(store.subscribe, store.getSnapshot, getServerSnapshot);
}

/** 视口 <= 1024px（平板 / 小笔记本及以下）。 */
export function useIsCompact(): boolean {
  return useBreakpoint('compact');
}

/** 视口 <= 768px（手机）。 */
export function useIsMobile(): boolean {
  return useBreakpoint('mobile');
}
