import { useMemo, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate, useOutletContext } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon, type IconName } from '../components/ui/Icon';
import { Drawer } from '../components/ui/Drawer';
import { GateCard } from '../components/ui/GateCard';
import { useAuth } from './auth';
import { api } from '../lib/api';
import { direction, gates as mockGates, type Gate, type GateStatus } from '../lib/mock';

interface NavEntry {
  to: string;
  no?: string;
  icon: IconName;
  zh: string;
  en: string;
}

const NAV_MAIN: NavEntry[] = [{ to: '/', icon: 'dashboard', zh: '总览', en: 'Dashboard' }];

const NAV_PIPE: NavEntry[] = [
  { to: '/wiki', no: '00', icon: 'book', zh: '文献追踪', en: 'Research Wiki' },
  { to: '/forge', no: '01', icon: 'bulb', zh: 'Idea 生成', en: 'Idea Forge' },
  { to: '/review', no: '02', icon: 'scale', zh: 'Idea 评审', en: 'Idea Review' },
  { to: '/experiment', no: '03', icon: 'flask', zh: '实验搭建', en: 'Experiment Lab' },
  { to: '/writer', no: '04', icon: 'pen', zh: '论文撰写', en: 'Paper Writer' },
  { to: '/paper-review', no: '05', icon: 'shield', zh: '论文评审', en: 'Paper Review' },
];

const CRUMBS: Record<string, [string, string]> = {
  '/': ['Polaris', '总览'],
  '/wiki': ['Stage 00', '文献追踪'],
  '/forge': ['Stage 01', 'Idea 生成'],
  '/review': ['Stage 02', 'Idea 评审'],
  '/experiment': ['Stage 03', '实验搭建'],
  '/writer': ['Stage 04', '论文撰写'],
  '/paper-review': ['Stage 05', '论文评审'],
  '/settings': ['Polaris', '设置'],
};

/** AppShell 通过 Outlet context 暴露给子页面的能力。 */
export interface ShellContext {
  /** 当前闸门列表（含本地 approve/reject 后的状态）。 */
  gates: Gate[];
  /** 打开审批抽屉，可选聚焦某个 gate。 */
  openGates: (gateId?: string | null) => void;
}

export function useShell(): ShellContext {
  return useOutletContext<ShellContext>();
}

function NavItem({ n }: { n: NavEntry }) {
  return (
    <NavLink to={n.to} end={n.to === '/'} className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}>
      {n.no && <span className="stage-no mono">{n.no}</span>}
      <span className="nav-ic">
        <Icon name={n.icon} size={16} />
      </span>
      <span style={{ flex: 1 }}>{n.zh}</span>
    </NavLink>
  );
}

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const { logout } = useAuth();

  // —— 审批闸门（M1：纯前端本地状态） ——
  const [overrides, setOverrides] = useState<Record<string, GateStatus>>({});
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [expandedGate, setExpandedGate] = useState<string | null>(null);

  const gates = useMemo(
    () => mockGates.map((g) => ({ ...g, status: overrides[g.id] ?? g.status })),
    [overrides],
  );
  const pending = gates.filter((g) => g.status === 'pending');
  const decided = gates.filter((g) => g.status !== 'pending');

  function openGates(gateId?: string | null) {
    setExpandedGate(gateId ?? null);
    setDrawerOpen(true);
  }
  function decide(id: string, status: GateStatus) {
    setOverrides((o) => ({ ...o, [id]: status }));
  }

  // —— 当前用户（后端未起时静默降级） ——
  const { data: me } = useQuery({
    queryKey: ['me'],
    queryFn: () => api.me(),
    retry: false,
    staleTime: 60_000,
  });
  const avatarText = me?.email?.slice(0, 2).toUpperCase() ?? '研';

  const [c1, c2] = CRUMBS[location.pathname] ?? ['Polaris', '—'];

  const ctx: ShellContext = { gates, openGates };

  return (
    <div className="app">
      {/* —— 侧栏 —— */}
      <div className="sidebar">
        <div className="sb-brand">
          <div className="sb-logo">
            <Icon name="sparkle" size={15} style={{ color: '#fff' }} />
          </div>
          <div>
            <h1>Polaris</h1>
            <p>{direction.slug}</p>
          </div>
        </div>
        <div className="sb-scroll scroll">
          {NAV_MAIN.map((n) => (
            <NavItem key={n.to} n={n} />
          ))}
          <div className="sb-section">研究流水线 · Pipeline</div>
          {NAV_PIPE.map((n) => (
            <NavItem key={n.to} n={n} />
          ))}
        </div>
        <div className="sb-foot">
          <div className="av">{avatarText}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 12, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {me?.email ?? '研究员'}
            </div>
            <div style={{ fontSize: 10.5, color: 'var(--text-3)' }}>Researcher</div>
          </div>
          <button
            className="icon-btn"
            style={{ width: 28, height: 28, border: 'none', background: 'transparent' }}
            title="设置 Settings"
            onClick={() => navigate('/settings')}
          >
            <Icon name="settings" size={16} />
          </button>
          <button
            className="icon-btn"
            style={{ width: 28, height: 28, border: 'none', background: 'transparent' }}
            title="退出登录 Logout"
            onClick={() => {
              logout();
              navigate('/login');
            }}
          >
            <Icon name="logout" size={15} />
          </button>
        </div>
      </div>

      {/* —— 主列 —— */}
      <div className="main">
        <div className="topbar">
          <div className="crumb">
            <span>{c1}</span>
            <span className="sep">›</span>
            <b>{c2}</b>
          </div>
          <div className="spacer" />
          <div className="searchbox">
            <Icon name="search" size={14} />
            <span>搜索论文 / idea / 实验…</span>
            <span className="mono" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-4)' }}>⌘K</span>
          </div>
          <button className="icon-btn" onClick={() => openGates(null)} title="审批中心 Approvals">
            <Icon name="bell" size={16} />
            {pending.length > 0 && <span className="badge">{pending.length}</span>}
          </button>
        </div>
        <div className="content scroll">
          <Outlet context={ctx} />
        </div>
      </div>

      {/* —— 审批抽屉 —— */}
      <Drawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        title={
          <>
            <Icon name="gate" size={18} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: 15, fontWeight: 680 }}>审批中心</span>
          </>
        }
        sub="人在环闸门 · Human-in-the-loop gates"
      >
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="sb-section" style={{ padding: 0 }}>待处理 · {pending.length}</span>
        </div>
        <div className="col gap10" style={{ marginBottom: 24 }}>
          {pending.length > 0 ? (
            pending.map((g) => (
              <GateCard
                key={g.id}
                gate={g}
                expanded={expandedGate === g.id}
                onToggle={() => setExpandedGate(expandedGate === g.id ? null : g.id)}
                onDecide={decide}
              />
            ))
          ) : (
            <div className="empty" style={{ padding: 20 }}>没有待处理的审批 🎉</div>
          )}
        </div>
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="sb-section" style={{ padding: 0 }}>历史记录</span>
        </div>
        <div className="col gap10">
          {decided.map((g) => (
            <GateCard
              key={g.id}
              gate={g}
              expanded={expandedGate === g.id}
              onToggle={() => setExpandedGate(expandedGate === g.id ? null : g.id)}
              onDecide={decide}
            />
          ))}
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.5, marginTop: 20, padding: '0 2px' }}>
          M1 阶段审批仅为前端本地状态；后端就绪后将对接 Gate API 与 WebSocket 通知，审批后流水线从断点恢复。
        </div>
      </Drawer>
    </div>
  );
}
