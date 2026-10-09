import type { ReactNode } from 'react';

/* ============================================================
   设置式表单行：标签（+ 一行说明）在左，控件在右，单列排列、行间细分隔。
   外层用全局的 .settings-list 包住。新建实验 / 生成想法 / 深度生成 /
   假设探索这类短参数表单共用；长文本字段仍用 FormField 上下排。
   ============================================================ */

export function ConfigRow({
  label,
  hint,
  children,
}: {
  label: string;
  /** 一行说明（超过一行请写进文档） */
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="settings-row" style={{ minHeight: 40, padding: '10px 0' }}>
      <div className="settings-row-text">
        <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--text)' }}>{label}</div>
        {hint && (
          <div
            title={hint}
            style={{
              fontSize: 12,
              color: 'var(--text-3)',
              marginTop: 2,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {hint}
          </div>
        )}
      </div>
      <div style={{ flexShrink: 0, display: 'flex', alignItems: 'center', gap: 10 }}>{children}</div>
    </div>
  );
}

/** 行内滑块 + 当前值（ConfigRow 的右侧控件）。 */
export function RangeControl({
  value,
  min,
  max,
  step,
  format,
  onChange,
  ariaLabel,
}: {
  value: number;
  min: number;
  max: number;
  step: number;
  format?: (v: number) => string;
  onChange: (v: number) => void;
  ariaLabel: string;
}) {
  return (
    <>
      <input
        type="range"
        aria-label={ariaLabel}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: 160 }}
      />
      <span className="mono" style={{ fontSize: 12, fontWeight: 600, width: 36, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
        {format ? format(value) : value}
      </span>
    </>
  );
}
