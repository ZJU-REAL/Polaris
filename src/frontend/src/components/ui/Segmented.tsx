import type { ReactNode } from 'react';

export interface SegmentedOption<V extends string> {
  v: V;
  label: ReactNode;
}

export interface SegmentedProps<V extends string> {
  options: readonly (V | SegmentedOption<V>)[];
  value: V;
  onChange: (v: V) => void;
}

/** Segmented control（分段切换）。 */
export function Segmented<V extends string>({ options, value, onChange }: SegmentedProps<V>) {
  return (
    <div
      style={{
        display: 'inline-flex',
        background: 'var(--surface-2)',
        border: '0.5px solid var(--border-2)',
        borderRadius: 9,
        padding: 3,
        gap: 2,
      }}
    >
      {options.map((o) => {
        const v = typeof o === 'string' ? o : o.v;
        const label = typeof o === 'string' ? o : o.label;
        const on = v === value;
        return (
          <button
            key={v}
            type="button"
            onClick={() => onChange(v)}
            style={{
              border: 'none',
              cursor: 'pointer',
              borderRadius: 6,
              padding: '5px 13px',
              fontSize: 12.5,
              fontWeight: 600,
              fontFamily: 'var(--sans)',
              background: on ? 'var(--surface)' : 'transparent',
              color: on ? 'var(--text)' : 'var(--text-3)',
              boxShadow: on ? 'var(--shadow-card)' : 'none',
              transition: 'all .12s',
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
