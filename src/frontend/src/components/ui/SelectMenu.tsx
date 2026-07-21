import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react';
import { Icon } from './Icon';

/* ============================================================
   自绘下拉选择器（#98 起源于设置页，下沉为公共组件）：
   - SelectMenu：纯下拉（固定候选，不自由输入），替代原生 <select>
   - DropdownList / useClickOutside：与 ModelCombobox 等组合框共用的面板
   ============================================================ */

const COMBO_ITEM_H = 30;
const COMBO_VISIBLE = 5;

/** 打开状态下点击组件外部时收起。 */
export function useClickOutside(ref: RefObject<HTMLElement | null>, active: boolean, onAway: () => void) {
  useEffect(() => {
    if (!active) return;
    const onDocDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onAway();
    };
    document.addEventListener('mousedown', onDocDown);
    return () => document.removeEventListener('mousedown', onDocDown);
  }, [ref, active, onAway]);
}

/** 候选面板：最多可视 5 项、超出内部滚动；高亮项随键盘滚动到可见。 */
export function DropdownList({ items, hi, mono, onHover, onPick }: {
  items: { key: string; label: string }[];
  hi: number;
  /** 候选用等宽字体（模型名等代码标识符） */
  mono?: boolean;
  onHover: (i: number) => void;
  onPick: (i: number) => void;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    listRef.current?.children[hi]?.scrollIntoView({ block: 'nearest' });
  }, [hi]);
  return (
    <div
      ref={listRef}
      role="listbox"
      style={{
        position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 30, marginTop: 4,
        maxHeight: COMBO_ITEM_H * COMBO_VISIBLE, overflowY: 'auto',
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-sm)', boxShadow: 'var(--shadow-pop)',
      }}
    >
      {items.map((it, i) => (
        <div
          key={it.key}
          role="option"
          aria-selected={i === hi}
          className={mono ? 'mono' : undefined}
          style={{
            height: COMBO_ITEM_H, lineHeight: `${COMBO_ITEM_H}px`, padding: '0 10px', fontSize: 12,
            cursor: 'pointer', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            background: i === hi ? 'var(--accent-soft)' : 'transparent',
            color: i === hi ? 'var(--accent-text)' : 'var(--text)',
          }}
          onMouseEnter={() => onHover(i)}
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(i);
          }}
        >
          {it.label}
        </div>
      ))}
    </div>
  );
}

/** 纯下拉选择器（固定候选，不自由输入）：与 ModelCombobox 同一套面板视觉。 */
export function SelectMenu({ value, options, muted, disabled, placeholder, style, wrapStyle, onChange }: {
  value: string;
  options: { value: string; label: string }[];
  /** 「跟随默认」行的弱化样式 */
  muted?: boolean;
  /** 禁用（沿用 .input:disabled 视觉） */
  disabled?: boolean;
  /** value 为空且候选里没有空值项时展示的占位文案（对应原生 select 的 disabled 占位 option） */
  placeholder?: string;
  /** 覆盖触发按钮样式（如表格行内 height: 32） */
  style?: CSSProperties;
  /** 覆盖外层容器样式（行内布局给宽度用，如 width: 120） */
  wrapStyle?: CSSProperties;
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(0);
  const wrapRef = useRef<HTMLDivElement>(null);
  useClickOutside(wrapRef, open, () => setOpen(false));

  const selectedIdx = options.findIndex((o) => o.value === value);
  const selected = selectedIdx >= 0 ? options[selectedIdx] : undefined;

  const openList = () => {
    if (disabled || options.length === 0) return;
    setOpen(true);
    setHi(Math.max(selectedIdx, 0));
  };
  const pick = (i: number) => {
    const o = options[i];
    if (o === undefined) return;
    // 与原生 select 一致：重选当前值不触发 onChange（路由表里避免误清空 model）
    if (o.value !== value) onChange(o.value);
    setOpen(false);
  };
  const onKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (open) pick(hi);
      else openList();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (open) setHi((h) => Math.min(h + 1, options.length - 1));
      else openList();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (open) setHi((h) => Math.max(h - 1, 0));
      else openList();
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  return (
    <div ref={wrapRef} style={{ position: 'relative', ...wrapStyle }}>
      <button
        type="button"
        className="input"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        style={{
          width: '100%', textAlign: 'left', cursor: disabled ? 'not-allowed' : 'pointer',
          display: 'flex', alignItems: 'center', gap: 6,
          color: muted || !selected || selected.value === '' ? 'var(--text-3)' : 'var(--text)',
          ...style,
        }}
        onClick={() => (open ? setOpen(false) : openList())}
        onKeyDown={onKeyDown}
      >
        <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {selected?.label ?? (value === '' && placeholder !== undefined ? placeholder : value)}
        </span>
        <Icon name="chevDown" size={12}
          style={{ color: 'var(--text-3)', flexShrink: 0, ...(open ? { transform: 'rotate(180deg)' } : {}) }} />
      </button>
      {open && (
        <DropdownList
          items={options.map((o) => ({ key: o.value, label: o.label }))}
          hi={hi}
          onHover={setHi}
          onPick={pick}
        />
      )}
    </div>
  );
}
