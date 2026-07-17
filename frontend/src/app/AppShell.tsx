import { useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate, useOutletContext } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Avatar } from '../components/ui/Avatar';
import { Icon, type IconName } from '../components/ui/Icon';
import { PolarisMark, PolarisWordmark } from '../components/ui/PolarisLogo';
import { Drawer } from '../components/ui/Drawer';
import { GateCard, gateTitle } from '../components/ui/GateCard';
import { ToastHost, toast } from '../components/ui/Toast';
import { useAuth } from './auth';
import { useProject } from './project';
import { SearchPalette } from './SearchPalette';
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
  { to: '/forge', no: '01', icon: 'bulb', zh: '想法生成', en: 'Idea Forge' },
  { to: '/review', no: '02', icon: 'scale', zh: '想法评审', en: 'Idea Review' },
  { to: '/experiment', no: '03', icon: 'flask', zh: '实验搭建', en: 'Experiment Lab' },
  { to: '/writer', no: '04', icon: 'pen', zh: '论文撰写', en: 'Paper Writer' },
  { to: '/paper-review', no: '05', icon: 'shield', zh: '论文评审', en: 'Paper Review' },
];

// 阶段路径 → 功能权限键（管理员在设置里可禁用；被禁用的阶段从导航隐藏）
const FEATURE_BY_PATH: Record<string, string> = {
  '/forge': 'forge',
  '/review': 'review',
  '/experiment': 'experiment',
  '/writer': 'writer',
  '/paper-review': 'paper_review',
};

function crumbFor(pathname: string): [string, string] {
  if (pathname === '/') return ['Polaris', '总览'];
  if (pathname === '/projects/new') return ['研究方向', '新建方向'];
  if (pathname.startsWith('/projects/')) return ['研究方向', '方向详情'];
  if (pathname === '/voyages') return ['Polaris', 'AI 任务'];
  if (pathname.startsWith('/voyages/')) return ['AI 任务', '任务详情'];
  if (pathname.startsWith('/papers/')) return ['文献追踪', '论文阅读'];
  if (pathname.startsWith('/ideas/')) return ['想法生成', '想法详情'];
  if (pathname.startsWith('/join/')) return ['研究方向', '接受邀请'];
  if (pathname.startsWith('/experiment/')) return ['实验搭建', '实验详情'];
  if (pathname.startsWith('/writer/')) return ['论文撰写', '编辑工作台'];
  const table: Record<string, [string, string]> = {
    '/wiki': ['Stage 00', '文献追踪'],
    '/forge': ['Stage 01', '想法生成'],
    '/review': ['Stage 02', '想法评审'],
    '/experiment': ['Stage 03', '实验搭建'],
    '/writer': ['Stage 04', '论文撰写'],
    '/paper-review': ['Stage 05', '论文评审'],
    '/skills': ['Polaris', '技能'],
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

/* ============================================================
   顶栏研究方向切换器：胶囊触发器 + 卡片式下拉菜单。
   菜单含方向列表（当前项打勾 + 蓝点）、新建方向、方向详情入口。
   ============================================================ */
function DirectionSwitcher() {
  const navigate = useNavigate();
  const location = useLocation();
  const { projects, isLoading, currentProjectId, currentProject, setCurrentProjectId } = useProject();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // 点外面 / Esc 关闭
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const triggerLabel = currentProject?.name ?? (isLoading ? '加载中…' : '选择研究方向');

  const itemStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    width: '100%',
    padding: '7px 12px',
    border: 'none',
    background: 'transparent',
    cursor: 'pointer',
    fontSize: 12.5,
    fontFamily: 'var(--sans)',
    color: 'var(--text)',
    textAlign: 'left',
    borderRadius: 8,
  };

  return (
    <div ref={wrapRef} style={{ position: 'relative', marginLeft: 18 }}>
      {/* 触发器：胶囊（图标 + 当前方向名 + 折叠箭头） */}
      <button
        onClick={() => setOpen((o) => !o)}
        title="切换研究方向"
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 7,
          height: 32,
          maxWidth: 280,
          padding: '0 10px 0 11px',
          borderRadius: 16,
          border: `0.5px solid ${open ? 'var(--accent)' : 'var(--border-2)'}`,
          background: open ? 'var(--accent-soft)' : 'var(--surface)',
          cursor: 'pointer',
          fontFamily: 'var(--sans)',
          transition: 'border-color .12s, background .12s',
        }}
      >
        <Icon name="layers" size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />
        <span
          style={{
            fontSize: 12.5,
            fontWeight: 620,
            color: currentProject ? 'var(--text)' : 'var(--text-3)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            minWidth: 0,
          }}
        >
          {triggerLabel}
        </span>
        <Icon
          name="chevDown"
          size={12}
          style={{ color: 'var(--text-3)', flexShrink: 0, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
        />
      </button>

      {/* 下拉菜单 */}
      {open && (
        <div
          className="card"
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            left: 0,
            zIndex: 40,
            width: 300,
            padding: 6,
            boxShadow: 'var(--shadow-pop)',
            animation: 'fadeUp 0.12s ease',
          }}
        >
          <div className="mono" style={{ fontSize: 10, color: 'var(--text-4)', padding: '4px 12px 6px', letterSpacing: '0.06em' }}>
            研究方向 · DIRECTIONS
          </div>
          <div className="scroll" style={{ maxHeight: 320, overflowY: 'auto' }}>
            {projects.length === 0 && (
              <div style={{ padding: '10px 12px', fontSize: 12, color: 'var(--text-4)' }}>
                {isLoading ? '加载中…' : '还没有研究方向，先新建一个'}
              </div>
            )}
            {projects.map((p) => {
              const active = p.id === currentProjectId;
              return (
                <button
                  key={p.id}
                  style={{ ...itemStyle, background: active ? 'var(--accent-soft)' : 'transparent' }}
                  onMouseEnter={(e) => {
                    if (!active) e.currentTarget.style.background = 'var(--surface-2)';
                  }}
                  onMouseLeave={(e) => {
                    if (!active) e.currentTarget.style.background = 'transparent';
                  }}
                  onClick={() => {
                    setCurrentProjectId(p.id);
                    setOpen(false);
                    // 正在看某个方向的详情页时，切换方向同步跳到新方向的详情（URL 驱动的页面）
                    if (/^\/projects\/(?!new)/.test(location.pathname)) {
                      navigate(`/projects/${p.id}`);
                    }
                  }}
                >
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: '50%',
                      background: active ? 'var(--accent)' : 'var(--border-strong)',
                      flexShrink: 0,
                    }}
                  />
                  <span
                    style={{
                      flex: 1,
                      minWidth: 0,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      fontWeight: active ? 650 : 450,
                      color: active ? 'var(--accent-text)' : 'var(--text)',
                    }}
                    title={p.name}
                  >
                    {p.name}
                  </span>
                  {active && <Icon name="check" size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />}
                </button>
              );
            })}
          </div>
          <div className="hr" style={{ margin: '6px 4px' }} />
          <button
            style={{ ...itemStyle, color: 'var(--text-2)' }}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--surface-2)')}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            onClick={() => {
              setOpen(false);
              navigate('/projects/new');
            }}
          >
            <Icon name="plus" size={13} style={{ color: 'var(--text-3)' }} />
            新建方向
          </button>
          {currentProjectId && (
            <button
              style={{ ...itemStyle, color: 'var(--text-2)' }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--surface-2)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              onClick={() => {
                setOpen(false);
                navigate(`/projects/${currentProjectId}`);
              }}
            >
              <Icon name="settings" size={13} style={{ color: 'var(--text-3)' }} />
              当前方向详情与设置
            </button>
          )}
        </div>
      )}
    </div>
  );
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

  // —— 审批抽屉 ——
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [expandedGate, setExpandedGate] = useState<string | null>(null);

  // —— 全局搜索（⌘K / Ctrl+K）——
  const [searchOpen, setSearchOpen] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setSearchOpen((o) => !o);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

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
        if (msg.status === 'paused_gate') toast('任务等待审批 · voyage paused at gate', 'info');
        else if (msg.status === 'done') toast('任务完成 · voyage done', 'ok');
        else if (msg.status === 'failed') toast('任务失败 · voyage failed', 'error');
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
      } else if (msg.type === 'manuscript.status') {
        // 论文撰写页靠这里实时刷新（起草 writing→compiled 等流转），不再快轮询
        void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
        void queryClient.invalidateQueries({ queryKey: ['manuscript', msg.manuscript_id] });
      } else if (msg.type === 'experiment.status') {
        void queryClient.invalidateQueries({ queryKey: ['experiments'] });
        void queryClient.invalidateQueries({ queryKey: ['experiment', msg.experiment_id] });
        if (msg.status === 'awaiting_gate') toast('实验等待预算审批 · experiment awaiting gate', 'info');
        else if (msg.status === 'running') toast('实验正式运行中 · experiment running', 'info');
        else if (msg.status === 'done') toast('实验完成 · experiment done', 'ok');
        else if (msg.status === 'failed') toast('实验失败 · experiment failed', 'error');
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

  const [c1, c2] = crumbFor(location.pathname);

  const ctx: ShellContext = { pendingGates: pending, gatesError: pendingQuery.isError, openGates };

  return (
    <div className="app">
      {/* —— 侧栏 —— */}
      <div className="sidebar">
        <div className="sb-brand">
          <PolarisMark size={46} />
          <PolarisWordmark height={27} />
        </div>
        <div className="sb-scroll scroll">
          {NAV_MAIN.map((n) => (
            <NavItem key={n.to} n={n} />
          ))}
          <NavItem n={{ to: '/voyages', icon: 'compass', zh: 'AI 任务', en: 'Tasks' }} />
          <NavItem n={{ to: '/skills', icon: 'sparkle', zh: '技能', en: 'Skills' }} />

          <div className="sb-section">研究流水线 · Pipeline</div>
          {NAV_PIPE.filter((n) => {
            const key = FEATURE_BY_PATH[n.to];
            return key == null || me?.features?.[key] !== false;
          }).map((n) => (
            <NavItem key={n.to} n={n} />
          ))}
        </div>
        <div className="sb-foot">
          <Avatar userId={me?.id} hasAvatar={!!me?.has_avatar} name={me?.display_name || me?.email || '研'} size={26} />
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
          {/* —— 研究方向选择器 —— */}
          <DirectionSwitcher />
          <div className="spacer" />
          <div className="searchbox" role="button" tabIndex={0} onClick={() => setSearchOpen(true)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setSearchOpen(true); }}>
            <Icon name="search" size={14} />
            <span>搜索论文 / 想法 / 实验…</span>
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
        sub="人工审批 · Human-in-the-loop approvals"
      >
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="sb-section" style={{ padding: 0 }}>待处理 · {pending.length}</span>
        </div>
        <div className="col gap10" style={{ marginBottom: 24 }}>
          {pendingQuery.isError ? (
            <div className="empty" style={{ padding: 20 }}>
              无法加载审批列表（后端不可用）
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
            <div className="empty" style={{ padding: 20 }}>没有待处理的审批</div>
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
          批准与任务关联的审批后，对应任务将自动从断点恢复；拒绝则置为 failed。
        </div>
      </Drawer>

      {/* —— 全局搜索面板 —— */}
      <SearchPalette open={searchOpen} onClose={() => setSearchOpen(false)} />

      <ToastHost />
    </div>
  );
}
