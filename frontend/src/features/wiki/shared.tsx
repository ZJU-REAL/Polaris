import { useEffect, useState, type ReactNode } from 'react';
import type { ConceptCategory } from '../../lib/api';

/* ============================================================
   Research Wiki 页共享：概念类别配色、Section、防抖 hook。
   ============================================================ */

export interface CategoryMeta {
  zh: string;
  c: string;
  bg: string;
}

/** 概念类别 → 中文名 + 配色（token 变量，参考原型 CONCEPT_KIND）。 */
export const CONCEPT_CATEGORY: Record<ConceptCategory, CategoryMeta> = {
  method: { zh: '方法', c: 'var(--accent-text)', bg: 'var(--accent-soft)' },
  architecture: { zh: '架构', c: 'var(--info-tx)', bg: 'var(--info-bg)' },
  methodology: { zh: '方法论', c: 'var(--violet-tx)', bg: 'var(--violet-bg)' },
  problem: { zh: '问题', c: 'var(--danger-tx)', bg: 'var(--danger-bg)' },
  metric: { zh: '指标', c: 'var(--ok-tx)', bg: 'var(--ok-bg)' },
  dataset: { zh: '数据集', c: 'var(--warn-tx)', bg: 'var(--warn-bg)' },
  other: { zh: '其他', c: 'var(--text-2)', bg: 'var(--surface-3)' },
};

export function categoryMeta(cat: string): CategoryMeta {
  return CONCEPT_CATEGORY[cat as ConceptCategory] ?? CONCEPT_CATEGORY.other;
}

/** 详情页小节标题（左侧 accent 竖条）。 */
export function Section({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <div style={{ marginTop: 24 }}>
      <div className="row gap8" style={{ marginBottom: 10 }}>
        <span style={{ width: 3, height: 13, borderRadius: 2, background: 'var(--accent)' }} />
        <span style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--text)' }}>{title}</span>
      </div>
      {children}
    </div>
  );
}

/** 输入防抖（搜索框用）。 */
export function useDebounced<T>(value: T, delayMs = 350): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}

/** 列表检索输入框（含放大镜 icon）。 */
export function SearchInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <input
      className="input"
      style={{ height: 32, fontSize: 12.5, flex: 1, minWidth: 0 }}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder ?? '搜索…'}
      type="search"
    />
  );
}
