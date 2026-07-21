import { useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate, useOutletContext } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon, type IconName } from '../components/ui/Icon';
import { PolarisMark, PolarisWordmark } from '../components/ui/PolarisLogo';
import { Drawer } from '../components/ui/Drawer';
import { GateCard, gateTitle } from '../components/ui/GateCard';
import { ToastHost, toast } from '../components/ui/Toast';
import { useAuth } from './auth';
import { useProject } from './project';
import { SearchPalette } from './SearchPalette';
import { UserMenu } from './UserMenu';
import { FeedbackWidget } from '../features/feedback/FeedbackWidget';
import { api, getToken, type GateDecision, type GateRead, type ReviewMessageRead } from '../lib/api';
import { tr } from '../lib/i18n';
import { LangToggle } from '../components/ui/LangToggle';
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
  if (pathname === '/') return ['Polaris', tr('总览', 'Dashboard')];
  if (pathname === '/projects/new') return [tr('研究方向', 'Directions'), tr('新建方向', 'New direction')];
  if (pathname.startsWith('/projects/')) return [tr('研究方向', 'Directions'), tr('方向详情', 'Direction detail')];
  if (pathname === '/voyages') return ['Polaris', tr('任务', 'Tasks')];
  if (pathname.startsWith('/voyages/')) return [tr('任务', 'Tasks'), tr('任务详情', 'Task detail')];
  if (pathname.startsWith('/papers/')) return [tr('文献追踪', 'Research Wiki'), tr('论文阅读', 'Paper reading')];
  if (pathname.startsWith('/ideas/')) return [tr('想法生成', 'Idea Forge'), tr('想法详情', 'Idea detail')];
  if (pathname.startsWith('/join/')) return [tr('研究方向', 'Directions'), tr('接受邀请', 'Accept invite')];
  if (pathname.startsWith('/experiment/')) return [tr('实验搭建', 'Experiment Lab'), tr('实验详情', 'Experiment detail')];
  if (pathname.startsWith('/writer/')) return [tr('论文撰写', 'Paper Writer'), tr('编辑工作台', 'Editor workspace')];
  const table: Record<string, [string, string]> = {
    '/wiki': ['Stage 00', tr('文献追踪', 'Research Wiki')],
    '/forge': ['Stage 01', tr('想法生成', 'Idea Forge')],
    '/review': ['Stage 02', tr('想法评审', 'Idea Review')],
    '/experiment': ['Stage 03', tr('实验搭建', 'Experiment Lab')],
    '/writer': ['Stage 04', tr('论文撰写', 'Paper Writer')],
    '/paper-review': ['Stage 05', tr('论文评审', 'Paper Review')],
    '/mcp-tools': ['Polaris', 'MCP'],
    '/skills': ['Polaris', tr('技能', 'Skills')],
    '/settings': ['Polaris', tr('设置', 'Settings')],
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

  const triggerLabel = currentProject?.name ?? (isLoading ? tr('加载中…', 'Loading…') : tr('选择研究方向', 'Pick a direction'));

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
        title={tr('切换研究方向', 'Switch research direction')}
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
            {tr('研究方向', 'DIRECTIONS')}
          </div>
          <div className="scroll" style={{ maxHeight: 320, overflowY: 'auto' }}>
            {projects.length === 0 && (
              <div style={{ padding: '10px 12px', fontSize: 12, color: 'var(--text-4)' }}>
                {isLoading ? tr('加载中…', 'Loading…') : tr('还没有研究方向，先新建一个', 'No directions yet — create one first')}
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
            {tr('新建方向', 'New direction')}
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
              {tr('当前方向详情与设置', 'Current direction detail & settings')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function NavItem({ n }: { n: NavEntry }) {
  return (
    <NavLink
      to={n.to}
      end={n.to === '/'}
      className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}
      title={tr(n.zh, n.en)}
    >
      <span className="nav-ic">
        <Icon name={n.icon} size={18} />
      </span>
      <span className="nav-label" style={{ flex: 1 }}>{tr(n.zh, n.en)}</span>
    </NavLink>
  );
}

export function AppShell() {
  const location = useLocation();
  const queryClient = useQueryClient();

  // —— 审批抽屉 ——
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [expandedGate, setExpandedGate] = useState<string | null>(null);

  // —— 侧栏收起（图标轨道），记忆到 localStorage ——
  const [navCollapsed, setNavCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem('polaris.navCollapsed') === '1';
    } catch {
      return false;
    }
  });
  const toggleNav = () => {
    setNavCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem('polaris.navCollapsed', next ? '1' : '0');
      } catch {
        /* 隐私模式：仅本次会话生效 */
      }
      return next;
    });
  };

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
      toast(`${vars.decision === 'approve' ? tr('已批准', 'Approved') : tr('已拒绝', 'Rejected')}：${gateTitle(gate)}`, 'ok');
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      void queryClient.invalidateQueries({ queryKey: ['voyage'] });
    },
    onError: (err) => {
      toast(`${tr('审批失败', 'Approval failed')}：${err instanceof Error ? err.message : String(err)}`, 'error');
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
        toast(`${tr('新审批请求', 'New approval request')}：${gateTitle(msg.gate)}`, 'info');
      } else if (msg.type === 'gate.decided') {
        void queryClient.invalidateQueries({ queryKey: ['gates'] });
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      } else if (msg.type === 'voyage.status') {
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
        void queryClient.invalidateQueries({ queryKey: ['voyage', msg.voyage_id] });
        if (msg.status === 'paused_gate') toast(tr('任务等待审批', 'Task paused for approval'), 'info');
        else if (msg.status === 'done') toast(tr('任务完成', 'Task done'), 'ok');
        else if (msg.status === 'failed') toast(tr('任务失败', 'Task failed'), 'error');
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
        // 起草结束（离开 writing 态）→ 收起 AI 光标/状态条
        if (msg.status !== 'writing') {
          queryClient.setQueryData(['ai-writing', msg.manuscript_id], null);
        }
      } else if (msg.type === 'manuscript.ai_writing') {
        // AI 起草相位 → 写入本地缓存，撰写页据此画 AI 光标与状态条（无网络请求）。
        // done 不清空（避免节间状态条闪烁），整体收起交给 manuscript.status 离开 writing 态。
        queryClient.setQueryData(['ai-writing', msg.manuscript_id], {
          fileId: msg.file_id,
          section: msg.section,
          phase: msg.phase,
          at: Date.now(),
        });
      } else if (msg.type === 'experiment.status') {
        void queryClient.invalidateQueries({ queryKey: ['experiments'] });
        void queryClient.invalidateQueries({ queryKey: ['experiment', msg.experiment_id] });
        if (msg.status === 'awaiting_gate') toast(tr('实验等待预算审批', 'Experiment awaiting budget approval'), 'info');
        else if (msg.status === 'running') toast(tr('实验正式运行中', 'Experiment running'), 'info');
        else if (msg.status === 'done') toast(tr('实验完成', 'Experiment done'), 'ok');
        else if (msg.status === 'failed') toast(tr('实验失败', 'Experiment failed'), 'error');
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
    <div className={'app' + (navCollapsed ? ' nav-collapsed' : '')}>
      {/* —— 侧栏 —— */}
      <div className="sidebar">
        <div className="sb-brand">
          <PolarisMark size={41} />
          {/* 收起后只留左侧图形标：直接不渲染字标，杜绝溢出（不靠 CSS 隐藏） */}
          {!navCollapsed && <PolarisWordmark height={30} />}
        </div>
        <div className="sb-scroll scroll">
          {NAV_MAIN.map((n) => (
            <NavItem key={n.to} n={n} />
          ))}
          <NavItem n={{ to: '/voyages', icon: 'compass', zh: '任务', en: 'Tasks' }} />
          <NavItem n={{ to: '/mcp-tools', icon: 'server', zh: 'MCP', en: 'MCP' }} />
          <NavItem n={{ to: '/skills', icon: 'sparkle', zh: '技能', en: 'Skills' }} />

          <div className="sb-section">{tr('研究流水线', 'Pipeline')}</div>
          {NAV_PIPE.filter((n) => {
            const key = FEATURE_BY_PATH[n.to];
            return key == null || me?.features?.[key] !== false;
          }).map((n) => (
            <NavItem key={n.to} n={n} />
          ))}
        </div>
        <div className="sb-foot">
          <UserMenu me={me} collapsed={navCollapsed} />
        </div>
      </div>

      {/* —— 主列 —— */}
      <div className="main">
        <div className="topbar">
          <button
            className="icon-btn nav-toggle"
            onClick={toggleNav}
            title={navCollapsed ? tr('展开菜单栏', 'Expand sidebar') : tr('收起菜单栏', 'Collapse sidebar')}
            aria-label={navCollapsed ? tr('展开菜单栏', 'Expand sidebar') : tr('收起菜单栏', 'Collapse sidebar')}
          >
            <Icon name="sidebar" size={16} />
          </button>
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
            <span>{tr('搜索论文 / 想法 / 实验…', 'Search papers / ideas / experiments…')}</span>
            <span className="mono" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-4)' }}>⌘K</span>
          </div>
          <LangToggle />
          <button className="icon-btn" onClick={() => openGates(null)} title={tr('审批中心', 'Approvals')}>
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
            <span style={{ fontSize: 15, fontWeight: 680 }}>{tr('审批中心', 'Approvals')}</span>
          </>
        }
        sub={tr('人工审批', 'Human-in-the-loop approvals')}
      >
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="sb-section" style={{ padding: 0 }}>{tr('待处理', 'Pending')} · {pending.length}</span>
        </div>
        <div className="col gap10" style={{ marginBottom: 24 }}>
          {pendingQuery.isError ? (
            <div className="empty" style={{ padding: 20 }}>
              {tr('无法加载审批列表（后端不可用）', 'Failed to load approvals (backend unavailable)')}
              <div style={{ marginTop: 10 }}>
                <button className="btn btn-soft sm" onClick={() => void pendingQuery.refetch()}>
                  {tr('重试', 'Retry')}
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
            <div className="empty" style={{ padding: 20 }}>{tr('没有待处理的审批', 'No pending approvals')}</div>
          )}
        </div>
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="sb-section" style={{ padding: 0 }}>{tr('历史记录', 'History')}</span>
        </div>
        <div className="col gap10">
          {decidedQuery.isLoading ? (
            <div className="empty" style={{ padding: 16 }}>{tr('加载中…', 'Loading…')}</div>
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
            <div className="empty" style={{ padding: 16 }}>{tr('暂无历史审批记录', 'No past approvals')}</div>
          )}
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.5, marginTop: 20, padding: '0 2px' }}>
          {tr('批准与任务关联的审批后，对应任务将自动从断点恢复；拒绝则置为 failed。', 'Approving a task-linked request resumes the task from its checkpoint; rejecting marks it failed.')}
        </div>
      </Drawer>

      {/* —— 全局搜索面板 —— */}
      <SearchPalette open={searchOpen} onClose={() => setSearchOpen(false)} />

      {/* —— 全局反馈入口（右下角悬浮气泡） —— */}
      <FeedbackWidget />

      <ToastHost />
    </div>
  );
}
