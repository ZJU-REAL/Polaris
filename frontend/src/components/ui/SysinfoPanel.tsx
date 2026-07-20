import type { ReactNode } from 'react';
import { Icon, type IconName } from './Icon';
import { tr } from '../../lib/i18n';
import type { SshSysinfo } from '../../lib/api';

/* ============================================================
   服务器系统状态面板（共享组件）：分组展示 处理器 / 内存 /
   显卡 / 磁盘，带用量条。设置页与实验页共用；连接失败给明确
   提示，探测缺项防御式跳过。颜色全部取 tokens。
   ============================================================ */

/** MiB → 人话（GB 保留 1 位；小于 1GB 用 MB）。 */
export function fmtMib(mib: number | undefined): string {
  if (mib == null || !Number.isFinite(mib)) return '—';
  if (mib < 1024) return `${mib} MB`;
  return `${(mib / 1024).toFixed(1)} GB`;
}

/** 用量条：>90% 红、>70% 黄、其余主题色。 */
function UsageBar({ used, total }: { used: number; total: number }) {
  const pct = total > 0 ? Math.min(100, Math.max(0, (used / total) * 100)) : 0;
  const color = pct > 90 ? 'var(--danger)' : pct > 70 ? 'var(--warn)' : 'var(--accent)';
  return (
    <div style={{ height: 5, borderRadius: 3, background: 'var(--surface-3)', overflow: 'hidden' }}>
      <div style={{ width: `${pct}%`, height: '100%', borderRadius: 3, background: color }} />
    </div>
  );
}

function GroupCard({ icon, title, children }: { icon: IconName; title: string; children: ReactNode }) {
  return (
    <div style={{ background: 'var(--surface)', border: '0.5px solid var(--border)', borderRadius: 10, padding: '10px 14px' }}>
      <div className="row gap6" style={{ alignItems: 'center', marginBottom: 8 }}>
        <Icon name={icon} size={13} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: 12, fontWeight: 650, color: 'var(--text-2)' }}>{title}</span>
      </div>
      <div className="col gap8">{children}</div>
    </div>
  );
}

function MeterRow({ label, used, total, extra }: { label: string; used: number; total: number; extra?: string }) {
  return (
    <div className="col" style={{ gap: 3 }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-2)' }}>{label}</span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
          {fmtMib(used)} / {fmtMib(total)}{extra ? ` · ${extra}` : ''}
        </span>
      </div>
      <UsageBar used={used} total={total} />
    </div>
  );
}

export function SysinfoPanel({
  loading,
  error,
  info,
  onRefresh,
}: {
  loading: boolean;
  error: boolean;
  info: SshSysinfo | undefined;
  onRefresh: () => void;
}) {
  if (loading) return <div className="muted" style={{ fontSize: 12 }}>{tr('正在探测服务器…', 'Probing server…')}</div>;
  if (error || !info) {
    return (
      <div className="row gap8" style={{ alignItems: 'center' }}>
        <span className="muted" style={{ fontSize: 12 }}>{tr('获取失败', 'Failed to fetch')}</span>
        <button className="btn btn-soft sm" onClick={onRefresh}>{tr('重试', 'Retry')}</button>
      </div>
    );
  }
  if (!info.ok) {
    return (
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
          {tr('连接失败', 'Unreachable')}
        </span>
        <span className="mono muted" style={{ fontSize: 11, wordBreak: 'break-all' }}>{info.detail}</span>
        <button className="btn btn-soft sm" onClick={onRefresh}>{tr('重试', 'Retry')}</button>
      </div>
    );
  }
  const cpu = info.cpu ?? {};
  const mem = info.mem ?? {};
  const gpus = info.gpus ?? [];
  const disks = info.disks ?? [];
  return (
    <div className="col gap10">
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))', gap: 10 }}>
        {/* 处理器 */}
        <GroupCard icon="cpu" title={tr('处理器', 'CPU')}>
          <div className="row gap8" style={{ alignItems: 'baseline', flexWrap: 'wrap' }}>
            <span style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)' }}>{cpu.cores ?? '—'}</span>
            <span className="muted" style={{ fontSize: 11.5 }}>{tr('核', 'cores')}</span>
            {cpu.load_1m != null && (
              <span className="mono muted" style={{ fontSize: 11, marginLeft: 'auto' }}>
                {tr('负载', 'load')} {cpu.load_1m} / {cpu.load_5m ?? '—'} / {cpu.load_15m ?? '—'}
              </span>
            )}
          </div>
          {cpu.cores != null && cpu.load_1m != null && <UsageBar used={cpu.load_1m} total={cpu.cores} />}
        </GroupCard>

        {/* 内存 */}
        <GroupCard icon="layers" title={tr('内存', 'Memory')}>
          {mem.total_mib != null ? (
            <MeterRow
              label={tr('已用', 'used')}
              used={mem.used_mib ?? 0}
              total={mem.total_mib}
              extra={mem.available_mib != null ? `${tr('可用', 'avail')} ${fmtMib(mem.available_mib)}` : undefined}
            />
          ) : (
            <span className="muted" style={{ fontSize: 11.5 }}>{tr('未探测到', 'Not detected')}</span>
          )}
        </GroupCard>

        {/* 显卡 */}
        <GroupCard icon="chart" title={tr('显卡', 'GPUs')}>
          {gpus.length === 0 ? (
            <span className="muted" style={{ fontSize: 11.5 }}>{tr('无 GPU', 'No GPU')}</span>
          ) : (
            gpus.map((g) => (
              <MeterRow
                key={g.index}
                label={`GPU ${g.index}`}
                used={g.mem_total_mib - g.mem_free_mib}
                total={g.mem_total_mib}
                extra={`${tr('空闲', 'free')} ${fmtMib(g.mem_free_mib)}`}
              />
            ))
          )}
        </GroupCard>

        {/* 磁盘 */}
        <GroupCard icon="server" title={tr('磁盘', 'Disks')}>
          {disks.length === 0 ? (
            <span className="muted" style={{ fontSize: 11.5 }}>{tr('未探测到', 'Not detected')}</span>
          ) : (
            disks.map((d) => <MeterRow key={d.mount} label={d.mount} used={d.used_mib} total={d.total_mib} />)
          )}
        </GroupCard>
      </div>
      <div className="row">
        <button className="btn btn-soft sm" style={{ marginLeft: 'auto' }} onClick={onRefresh}>
          <Icon name="refresh" size={11} /> {tr('刷新', 'Refresh')}
        </button>
      </div>
    </div>
  );
}
