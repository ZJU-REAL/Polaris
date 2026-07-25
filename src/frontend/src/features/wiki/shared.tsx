import { useEffect, useId, useState, type ReactNode } from 'react';
import type { ConceptCategory } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { Icon } from '../../components/ui/Icon';
import { Switch } from '../../components/ui/Switch';

/* ============================================================
   Research Wiki 页共享：概念类别配色、Section、防抖 hook。
   ============================================================ */

export interface CategoryMeta {
  zh: string;
  en: string;
  c: string;
  bg: string;
}

/** 概念类别 → 中英文名 + 配色（token 变量，参考原型 CONCEPT_KIND）。
    文案在渲染处用 tr(meta.zh, meta.en) 取当前语言。 */
export const CONCEPT_CATEGORY: Record<ConceptCategory, CategoryMeta> = {
  method: { zh: '方法', en: 'Method', c: 'var(--accent-text)', bg: 'var(--accent-soft)' },
  architecture: { zh: '架构', en: 'Architecture', c: 'var(--info-tx)', bg: 'var(--info-bg)' },
  methodology: { zh: '方法论', en: 'Methodology', c: 'var(--violet-tx)', bg: 'var(--violet-bg)' },
  problem: { zh: '问题', en: 'Problem', c: 'var(--danger-tx)', bg: 'var(--danger-bg)' },
  metric: { zh: '指标', en: 'Metric', c: 'var(--ok-tx)', bg: 'var(--ok-bg)' },
  dataset: { zh: '数据集', en: 'Dataset', c: 'var(--warn-tx)', bg: 'var(--warn-bg)' },
  other: { zh: '其他', en: 'Other', c: 'var(--text-2)', bg: 'var(--surface-3)' },
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

/** 触发浏览器下载一个 blob（导出 zip / .bib / .json 用）。 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
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
      placeholder={placeholder ?? tr('搜索…', 'Search…')}
      type="search"
    />
  );
}

/** 语义检索开关（搜索框右边、高级检索按钮左边）：短标签 + Switch。
    四处检索条统一用它，别各写各的 chip。关掉=关键词字面匹配，打开=按意思找。 */
export function SemanticSwitch({
  checked,
  onChange,
  disabled,
  title,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  /** 悬浮说明（如「浏览记录不支持语义检索」）；不传给默认解释 */
  title?: string;
}) {
  const labelId = `${useId()}-semantic`;
  return (
    <div
      className="row gap6"
      style={{ flexShrink: 0, opacity: disabled ? 0.5 : 1 }}
      title={
        title ??
        tr('打开后按意思检索，而不是字面匹配关键词', 'Search by meaning instead of literal keyword matching')
      }
    >
      <span
        id={labelId}
        onClick={() => !disabled && onChange(!checked)}
        style={{
          fontSize: 11.5,
          whiteSpace: 'nowrap',
          userSelect: 'none',
          cursor: disabled ? 'not-allowed' : 'pointer',
          color: checked ? 'var(--accent-text)' : 'var(--text-3)',
        }}
      >
        {tr('语义检索', 'Semantic')}
      </span>
      <Switch checked={checked} onChange={onChange} disabled={disabled} aria-labelledby={labelId} />
    </div>
  );
}

/* ============================================================
   高级检索：轻量通用受控件（相关研究 / 我的文献库共用）。
   ============================================================ */

/** 年份字符串 → 整数（无效返回 undefined，用于组装查询参数）。 */
export function parseYear(v: string): number | undefined {
  const s = v.trim();
  if (!s) return undefined;
  const n = Number.parseInt(s, 10);
  return Number.isFinite(n) ? n : undefined;
}

/** 高级检索开关按钮（放大镜旁的 sliders 图标，激活时右上角小圆点）。 */
export function AdvancedToggle({
  open,
  active,
  onToggle,
  title,
}: {
  open: boolean;
  active: boolean;
  onToggle: () => void;
  title?: string;
}) {
  return (
    <button
      className="icon-btn"
      style={{
        width: 28,
        height: 28,
        flexShrink: 0,
        position: 'relative',
        ...(open || active ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : {}),
      }}
      title={title ?? tr('高级检索', 'Advanced search')}
      onClick={onToggle}
    >
      <Icon name="sliders" size={14} />
      {active && (
        <span
          style={{
            position: 'absolute',
            top: 3,
            right: 3,
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: 'var(--accent)',
          }}
        />
      )}
    </button>
  );
}

/** 高级检索：可展开的紧凑面板容器（surface-2 底、竖排字段）。 */
export function AdvancedPanel({ children, onClear }: { children: ReactNode; onClear?: () => void }) {
  return (
    <div
      className="col gap8"
      style={{ marginTop: 8, padding: '10px 12px', borderRadius: 10, background: 'var(--surface-2)' }}
    >
      {children}
      {onClear && (
        <button
          className="btn btn-ghost sm"
          style={{ alignSelf: 'flex-start', height: 22, fontSize: 10.5 }}
          onClick={onClear}
        >
          {tr('清空高级条件', 'Clear advanced filters')}
        </button>
      )}
    </div>
  );
}

/** 高级检索：紧凑文本输入（作者 / 机构 / venue 等）。 */
export function FilterInput({
  value,
  onChange,
  placeholder,
  title,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  title?: string;
}) {
  return (
    <input
      className="input"
      style={{ flex: 1, minWidth: 0, height: 28, fontSize: 11.5 }}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      title={title}
    />
  );
}

/** 高级检索：年份区间（左侧固定宽度标签 + 两个 number 输入）。 */
export function YearRangeField({
  label,
  from,
  to,
  onFrom,
  onTo,
}: {
  label: string;
  from: string;
  to: string;
  onFrom: (v: string) => void;
  onTo: (v: string) => void;
}) {
  return (
    <div className="row gap6" style={{ fontSize: 11, color: 'var(--text-3)' }}>
      <span style={{ width: 52, flexShrink: 0 }}>{label}</span>
      <input
        className="input"
        type="number"
        inputMode="numeric"
        placeholder={tr('起始', 'From')}
        style={{ flex: 1, minWidth: 0, height: 26, fontSize: 11 }}
        value={from}
        onChange={(e) => onFrom(e.target.value)}
      />
      <span>—</span>
      <input
        className="input"
        type="number"
        inputMode="numeric"
        placeholder={tr('至今', 'To')}
        style={{ flex: 1, minWidth: 0, height: 26, fontSize: 11 }}
        value={to}
        onChange={(e) => onTo(e.target.value)}
      />
    </div>
  );
}
