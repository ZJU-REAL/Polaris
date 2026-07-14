import { useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate, useOutletContext } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon, type IconName } from '../components/ui/Icon';
import { Drawer } from '../components/ui/Drawer';
import { GateCard, gateTitle } from '../components/ui/GateCard';
import { ToastHost, toast } from '../components/ui/Toast';
import { useAuth } from './auth';
import { useProject } from './project';
import { api, getToken, isAdmin, type GateDecision, type GateRead, type ReviewMessageRead } from '../lib/api';
import { connectNotifications } from '../lib/ws';

interface NavEntry {
  to: string;
  no?: string;
  icon: IconName;
  zh: string;
  en: string;
}

const NAV_MAIN: NavEntry[] = [
  { to: '/', icon: 'dashboard', zh: '总览', en: 'Dashboard' },
];

const NAV_PIPE: NavEntry[] = [
  { to: '/wiki', no: '00', icon: 'book', zh: '文献追踪', en: 'Research Wiki' },
  { to: '/forge', no: '01', icon: 'bulb', zh: 'Idea 生成', en: 'Idea Forge' },
  { to: '/review', no: '02', icon: 'scale', zh: 'Idea 评审', en: 'Idea Review' },
  { to: '/experiment', no: '03', icon: 'flask', zh: '实验搭建', en: 'Experiment Lab' },
  { to: '/writer', no: '04', icon: 'pen', zh: '论文撰写', en: 'Paper Writer' },
  { to: '/paper-review', no: '05', icon: 'shield', zh: '论文评审', en: 'Paper Review' },
];

function crumbFor(pathname: string): [string, string] {
  if (pathname === '/') return ['Polaris', '总览'];
  if (pathname === '/projects/new') return ['研究方向', '新建方向'];
  if (pathname.startsWith('/projects/')) return ['研究方向', '方向详情'];
  if (pathname === '/voyages') return ['Polaris', '任务航程'];
  if (pathname.startsWith('/voyages/')) return ['任务航程', '航程详情'];
  if (pathname.startsWith('/ideas/')) return ['Idea Forge', 'Idea 详情'];
  const table: Record<string, [string, string]> = {
    '/wiki': ['Stage 00', '文献追踪'],
    '/forge': ['Stage 01', 'Idea 生成'],
    '/review': ['Stage 02', 'Idea 评审'],
    '/experiment': ['Stage 03', '实验搭建'],
    '/writer': ['Stage 04', '论文撰写'],
    '/paper-review': ['Stage 05', '论文评审'],
    '/settings': ['Polaris', '设置'],
  };
  return table[pathname] ?? ['Polaris', '—'];
}

/** AppShell 通过 Outlet context 暴露给子页面的能力。 */
export interface ShellContext {
  /** 待处理闸门（真实 API）。 */
  pendingGates: GateRead[];
  /** 闸门列表是否加载失败（后端未起）。 */
  gatesError: boolean;
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
  const queryClient = useQueryClient();
  const { projects, isLoading: projectsLoading, currentProjectId, currentProject, setCurrentProjectId } = useProject();

  // —— 审批抽屉 ——
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [expandedGate, setExpandedGate] = useState<string | null>(null);

  // —— 闸门（真实 API，后端未起时优雅降级为空列表 + 提示） ——
  const pendingQuery = useQuery({
    queryKey: ['gates', 'pending'],
    queryFn: () => api.listGates('pending'),
    retry: false,
    refetchInterval: 60_000,
  });
  const decidedQuery = useQuery({
    queryKey: ['gates', 'decided'],
    queryFn: () => api.listGates('decided'),
    retry: false,
    enabled: drawerOpen,
  });
  const pending = pendingQuery.data ?? [];
  const decided = decidedQuery.data ?? [];

  const decideMutation = useMutation({
    mutationFn: ({ id, decision, comment }: { id: string; decision: GateDecision; comment?: string }) =>
      api.decideGate(id, decision, comment),
    onSuccess: (gate, vars) => {
      toast(`已${vars.decision === 'approve' ? '批准' : '拒绝'}：${gateTitle(gate)}`, 'ok');
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      void queryClient.invalidateQueries({ queryKey: ['voyage'] });
    },
    onError: (err) => {
      toast(`审批失败：${err instanceof Error ? err.message : String(err)}`, 'error');
    },
  });

  function openGates(gateId?: string | null) {
    setExpandedGate(gateId ?? null);
    setDrawerOpen(true);
  }
  function decide(id: string, decision: GateDecision, comment?: string) {
    decideMutation.mutate({ id, decision, comment });
  }

  // —— WebSocket 通知：gate/voyage 事件 → invalidate + toast ——
  const { token } = useAuth();
  useEffect(() => {
    if (!token) return;
    const close = connectNotifications(getToken, (msg) => {
      if (msg.type === 'gate.created') {
        void queryClient.invalidateQueries({ queryKey: ['gates'] });
        toast(`新审批请求：${gateTitle(msg.gate)}`, 'info');
      } else if (msg.type === 'gate.decided') {
        void queryClient.invalidateQueries({ queryKey: ['gates'] });
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      } else if (msg.type === 'voyage.status') {
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
        void queryClient.invalidateQueries({ queryKey: ['voyage', msg.voyage_id] });
        if (msg.status === 'paused_gate') toast('航程等待审批 · voyage paused at gate', 'info');
        else if (msg.status === 'done') toast('航程完成 · voyage done', 'ok');
        else if (msg.status === 'failed') toast('航程失败 · voyage failed', 'error');
      } else if (msg.type === 'review.message') {
        // 正在看该 session 的组件共享此 query cache → 直接乐观追加（按 id 去重）
        queryClient.setQueryData<ReviewMessageRead[]>(['session-messages', msg.session_id], (old) =>
          old === undefined ? undefined : old.some((m) => m.id === msg.message.id) ? old : [...old, msg.message],
        );
        void queryClient.invalidateQueries({ queryKey: ['idea-sessions'] });
      } else if (msg.type === 'idea.status') {
        void queryClient.invalidateQueries({ queryKey: ['ideas'] });
        void queryClient.invalidateQueries({ queryKey: ['idea', msg.idea_id] });
        void queryClient.invalidateQueries({ queryKey: ['leaderboard'] });
        void queryClient.invalidateQueries({ queryKey: ['forge-state'] });
      }
    });
    return close;
  }, [token, queryClient]);

  // —— 当前用户（后端未起时静默降级） ——
  const { data: me } = useQuery({
    queryKey: ['me'],
    queryFn: () => api.me(),
    retry: false,
    staleTime: 60_000,
  });
  const avatarText = me?.email?.slice(0, 2).toUpperCase() ?? '研';

  const [c1, c2] = crumbFor(location.pathname);

  const ctx: ShellContext = { pendingGates: pending, gatesError: pendingQuery.isError, openGates };

  return (
    <div className="app">
      {/* —— 侧栏 —— */}
      <div className="sidebar">
        <div className="sb-brand">
          <div className="sb-logo">
            <Icon name="sparkle" size={15} style={{ color: '#fff' }} />
          </div>
          <div style={{ minWidth: 0 }}>
            <h1>Polaris</h1>
            <p style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {currentProject?.name ?? 'autonomous research'}
            </p>
          </div>
        </div>
        <div className="sb-scroll scroll">
          {NAV_MAIN.map((n) => (
            <NavItem key={n.to} n={n} />
          ))}
          <NavItem n={{ to: '/voyages', icon: 'compass', zh: '任务航程', en: 'Voyages' }} />

          <div className="sb-section">研究方向 · Directions</div>
          {projectsLoading && <div style={{ padding: '4px 10px', fontSize: 12, color: 'var(--text-4)' }}>加载中…</div>}
          {projects.map((p) => (
            <NavLink
              key={p.id}
              to={`/projects/${p.id}`}
              className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}
              onClick={() => setCurrentProjectId(p.id)}
              title={p.name}
            >
              <span className="nav-ic">
                <Icon name="layers" size={15} />
              </span>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.name}</span>
              {p.id === currentProjectId && (
                <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--accent)', flexShrink: 0 }} />
              )}
            </NavLink>
          ))}
          <NavLink to="/projects/new" className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}>
            <span className="nav-ic">
              <Icon name="plus" size={15} />
            </span>
            <span style={{ flex: 1, color: 'var(--text-3)' }}>新建方向</span>
          </NavLink>

          <div className="sb-section">研究流水线 · Pipeline</div>
          {NAV_PIPE.map((n) => (
            <NavItem key={n.to} n={n} />
          ))}
        </div>
        <div className="sb-foot">
          <div className="av">{avatarText}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 12, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {me?.display_name ?? me?.email ?? '研究员'}
            </div>
            <div style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{isAdmin(me) ? 'Admin' : 'Researcher'}</div>
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
          {pendingQuery.isError ? (
            <div className="empty" style={{ padding: 20 }}>
              无法加载闸门列表（后端不可用）
              <div style={{ marginTop: 10 }}>
                <button className="btn btn-soft sm" onClick={() => void pendingQuery.refetch()}>
                  重试 retry
                </button>
              </div>
            </div>
          ) : pending.length > 0 ? (
            pending.map((g) => (
              <GateCard
                key={g.id}
                gate={g}
                expanded={expandedGate === g.id}
                onToggle={() => setExpandedGate(expandedGate === g.id ? null : g.id)}
                onDecide={decide}
                deciding={decideMutation.isPending}
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
          {decidedQuery.isLoading ? (
            <div className="empty" style={{ padding: 16 }}>加载中…</div>
          ) : decided.length > 0 ? (
            decided.map((g) => (
              <GateCard
                key={g.id}
                gate={g}
                expanded={expandedGate === g.id}
                onToggle={() => setExpandedGate(expandedGate === g.id ? null : g.id)}
                onDecide={decide}
              />
            ))
          ) : (
            <div className="empty" style={{ padding: 16 }}>暂无历史审批记录</div>
          )}
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.5, marginTop: 20, padding: '0 2px' }}>
          批准带 voyage 的闸门后，对应航程将自动从断点恢复；拒绝则置为 failed。
        </div>
      </Drawer>

      <ToastHost />
    </div>
  );
}
