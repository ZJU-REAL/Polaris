export interface ScoreRingProps {
  value: number;
  max?: number;
  size?: number;
  label?: string;
}

/** Conic-gradient score ring, colored by threshold. */
export function ScoreRing({ value, max = 10, size = 46, label }: ScoreRingProps) {
  const pct = Math.max(0, Math.min(1, value / max));
  const deg = pct * 360;
  const color = value >= 7.5 ? 'var(--ok)' : value >= 6 ? 'var(--accent)' : 'var(--warn)';
  return (
    <div style={{ position: 'relative', width: size, height: size, flexShrink: 0 }}>
      <div
        style={{
          width: size,
          height: size,
          borderRadius: '50%',
          background: `conic-gradient(${color} ${deg}deg, var(--surface-3) ${deg}deg)`,
        }}
      />
      <div
        style={{
          position: 'absolute',
          inset: 4,
          borderRadius: '50%',
          background: 'var(--surface)',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <span style={{ fontFamily: 'var(--mono)', fontSize: size * 0.3, fontWeight: 700, color: 'var(--text)' }}>
          {value.toFixed(1)}
        </span>
        {label && <span style={{ fontSize: 8, color: 'var(--text-3)', marginTop: -1 }}>{label}</span>}
      </div>
    </div>
  );
}
