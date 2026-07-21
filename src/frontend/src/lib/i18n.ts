import { useSyncExternalStore } from 'react';

/* ============================================================
   轻量 i18n：全站单语显示（中文 / English），顶栏切换。
   - 业务代码用 tr('中文', 'English') 包住用户可见文案即可，无需接 hook；
     切换语言时 App 以 key={lang} 整树重挂载，所有 tr() 重新求值。
   - 注意：模块级常量里的文案在 import 时求值，不会随切换更新——
     含文案的常量要么写成函数，要么保留 {zh, en} 字段在渲染处再 tr。
   ============================================================ */

export type Lang = 'zh' | 'en';

const STORAGE_KEY = 'polaris.lang';

function readStored(): Lang {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === 'en' ? 'en' : 'zh';
  } catch {
    return 'zh';
  }
}

let current: Lang = readStored();
const listeners = new Set<() => void>();

export function getLang(): Lang {
  return current;
}

export function setLang(lang: Lang): void {
  if (lang === current) return;
  current = lang;
  try {
    localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    /* 隐私模式等场景写不进去：仅本次会话生效 */
  }
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** 响应式读当前语言（App 根部用它触发整树重挂载）。 */
export function useLang(): Lang {
  return useSyncExternalStore(subscribe, getLang);
}

/** 按当前语言取文案；英文缺失时回退中文。 */
export function tr(zh: string, en?: string): string {
  return current === 'en' && en ? en : zh;
}
