import { Icon } from './Icon';
import { tr } from '../../lib/i18n';
import type { SshSysinfo } from '../../lib/api';

/* ============================================================
   服务器系统状态面板（共享组件）：CPU / 内存 / GPU / 磁盘。
   设置页（按凭据）与实验页（按实验所在服务器）共用；
   连接失败给明确提示，探测缺项防御式跳过。
   ============================================================ */

/** MiB → 人话（GB 保留 1 位；小于 1GB 用 MB）。 */
export function fmtMib(mib: number | undefined): string {
  if (mib == null || !Number.isFinite(mib)) return '—';
  if (mib < 1024) return `${mib} MB`;
  return `${(mib / 1024).toFixed(1)} GB`;
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
    <div className="col gap8">
      <div className="row gap8" style={{ flexWrap: 'wrap', alignItems: 'center' }}>
        {cpu.cores != null && (
          <span className="pill sm mono" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
            <Icon name="cpu" size={11} /> CPU {cpu.cores} {tr('核', 'cores')}
            {cpu.load_1m != null && <span style={{ opacity: 0.75 }}> · {tr('负载', 'load')} {cpu.load_1m}</span>}
          </span>
        )}
        {mem.total_mib != null && (
          <span className="pill sm mono" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
            <Icon name="layers" size={11} /> {tr('内存', 'Mem')} {fmtMib(mem.used_mib)} / {fmtMib(mem.total_mib)}
            {mem.available_mib != null && <span style={{ opacity: 0.75 }}> · {tr('可用', 'avail')} {fmtMib(mem.available_mib)}</span>}
          </span>
        )}
        {gpus.map((g) => (
          <span key={g.index} className="pill sm mono" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            GPU{g.index} {tr('空闲', 'free')} {fmtMib(g.mem_free_mib)} / {fmtMib(g.mem_total_mib)}
          </span>
        ))}
        {gpus.length === 0 && (
          <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-4)' }}>
            {tr('无 GPU', 'No GPU')}
          </span>
        )}
        <button className="btn btn-soft sm" style={{ marginLeft: 'auto' }} onClick={onRefresh}>
          <Icon name="refresh" size={11} /> {tr('刷新', 'Refresh')}
        </button>
      </div>
      {disks.length > 0 && (
        <div className="row gap8" style={{ flexWrap: 'wrap' }}>
          {disks.map((d) => (
            <span key={d.mount} className="pill sm mono" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
              <Icon name="server" size={11} /> {d.mount} {tr('已用', 'used')} {fmtMib(d.used_mib)} / {fmtMib(d.total_mib)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
