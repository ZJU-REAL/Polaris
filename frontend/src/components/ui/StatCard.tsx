import { Icon, type IconName } from './Icon';
import { tr } from '../../lib/i18n';

export interface StatCardProps {
  icon: IconName;
  label: string;
  en: string;
  value: string | number;
  sub?: string;
  accent?: boolean;
}

/** Dashboard 指标卡。 */
export function StatCard({ icon, label, en, value, sub, accent }: StatCardProps) {
  return (
    <div className="card card-pad" style={{ flex: 1 }}>
      <div className="row gap10" style={{ marginBottom: 12 }}>
        <div
          style={{
            width: 30,
            height: 30,
            borderRadius: 8,
            background: accent ? 'var(--accent-soft)' : 'var(--surface-2)',
            color: accent ? 'var(--accent)' : 'var(--text-2)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <Icon name={icon} size={16} />
        </div>
        <div>
          <div style={{ fontSize: 12.5, fontWeight: 600 }}>{tr(label, en)}</div>
        </div>
      </div>
      <div className="row" style={{ alignItems: 'baseline', gap: 8 }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 28, fontWeight: 700, letterSpacing: '-0.02em' }}>
          {value}
        </span>
        {sub && <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{sub}</span>}
      </div>
    </div>
  );
}
