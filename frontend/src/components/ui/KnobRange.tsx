import { FormField } from './FormField';

export interface KnobRangeProps {
  label: string;
  en: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format?: (v: number) => string;
  onChange: (v: number) => void;
}

/** 成本旋钮：range slider + 当前值（FormField 包装）。 */
export function KnobRange({ label, en, hint, value, min, max, step, format, onChange }: KnobRangeProps) {
  return (
    <FormField label={label} en={en} hint={hint}>
      <div className="row gap12">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          style={{ flex: 1 }}
        />
        <span className="mono" style={{ fontSize: 12.5, fontWeight: 650, width: 44, textAlign: 'right' }}>
          {format ? format(value) : value}
        </span>
      </div>
    </FormField>
  );
}
