import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { Link, NavLink, Outlet, useLocation, useNavigate, useOutletContext } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon, type IconName } from '../components/ui/Icon';
import { PolarisMark, PolarisWordmark } from '../components/ui/PolarisLogo';
import { Drawer } from '../components/ui/Drawer';
import { GateCard, gateTitle } from '../components/ui/GateCard';
import { ToastHost, toast } from '../components/ui/Toast';
import { UpdateBadge } from '../components/ui/UpdateBadge';
import { useAuth } from './auth';
import { topicPath, useProject } from './project';
import { AssistantPanel } from '../features/assistant/AssistantPanel';
import { BuddyDock, dockMaxWidth } from '../features/assistant/BuddyDock';
import { alreadyNudgedToday, markNudged } from '../features/assistant/nudge';
import { PAPER_DND_MIME } from '../features/assistant/paperDrag';
import { SearchPalette } from './SearchPalette';
import { UserMenu } from './UserMenu';
import { FeedbackWidget } from '../features/feedback/FeedbackWidget';
import { api, getToken, isAdmin, isLabScopedTask, type GateDecision, type GateRead, type ReviewMessageRead } from '../lib/api';
import { tr } from '../lib/i18n';
import { LangToggle } from '../components/ui/LangToggle';
import { connectNotifications } from '../lib/ws';
import { useIsMobile } from '../lib/useBreakpoint';
import { notifyDesktop } from '../lib/desktop-notify';
import { setBadgeCount } from '../lib/host';

interface NavEntry {
  /** 非课题作用域页面的绝对路径（与 sub 二选一）。 */
  to?: string;
  /** 课题作用域子路径（渲染时经 topicPath 拼 /t/<id> 前缀）；to 与 sub 都缺省 = 工作台。 */
  sub?: string;
  /** 覆盖高亮匹配：true 时仅精确匹配路径（工作台标签项指向 /t/<id>?tab=… 时用，避免常亮）。 */
  end?: boolean;
  no?: string;
  icon: IconName;
  zh: string;
  en: string;
}

/** 侧栏三个分组的标题：面包屑第一段直接复用，保证两处永远对得上。 */
const NAV_GROUPS = {
  lab: { zh: '实验室', en: 'Lab' },
  topic: { zh: '课题研究', en: 'Topic' },
  personal: { zh: '个人', en: 'Personal' },
} as const;

// 实验室级导航：跨课题的公共资产（P5c 起为共享方向文献库列表）
const NAV_LAB: NavEntry[] = [
  { to: '/lab', icon: 'flask', zh: '实验室工作台', en: 'Lab Workbench' },
  { to: '/libraries', icon: 'book', zh: '文献库', en: 'Libraries' },
  { to: '/daily', icon: 'heart', zh: '每日新论文', en: 'Daily Papers' },
];

const NAV_MAIN: NavEntry[] = [
  { icon: 'dashboard', zh: '课题工作台', en: 'Topic Workbench' },
  { sub: 'research', icon: 'pin', zh: '相关研究', en: 'Related Work' },
];

// 个人区：跨课题的个人页面（设置入口在底部头像菜单里，不重复占位）
const NAV_PERSONAL: NavEntry[] = [
  { to: '/library', icon: 'bookmark', zh: '我的文献库', en: 'My Library' },
  { to: '/skills', icon: 'sparkle', zh: '技能', en: 'Skills' },
];

const NAV_PIPE: NavEntry[] = [
  { sub: 'forge', no: '01', icon: 'bulb', zh: '想法生成', en: 'Idea Forge' },
  { sub: 'review', no: '02', icon: 'scale', zh: '想法评审', en: 'Idea Review' },
  { sub: 'experiment', no: '03', icon: 'flask', zh: '实验搭建', en: 'Experiment Lab' },
  { sub: 'writer', no: '04', icon: 'pen', zh: '论文撰写', en: 'Paper Writer' },
  { sub: 'paper-review', no: '05', icon: 'shield', zh: '论文评审', en: 'Paper Review' },
];

// 阶段子路径 → 功能权限键（管理员在设置里可禁用；被禁用的阶段从导航隐藏）
const FEATURE_BY_SUB: Record<string, string> = {
  forge: 'forge',
  review: 'review',
  experiment: 'experiment',
  writer: 'writer',
  'paper-review': 'paper_review',
};

/** 面包屑一段；没有 to = 当前页（最后一段），不可点。 */
interface Crumb {
  label: string;
  to?: string;
}

/**
 * 侧栏条目 → 面包屑段：文案与跳转都取自 NAV_* 那一份定义，
 * key 用条目的 to（实验室 / 个人）或 sub（课题作用域），课题工作台的 key 是空串。
 */
function navCrumb(key: string, pid: string | null): Crumb {
  const entry = [...NAV_LAB, ...NAV_MAIN, ...NAV_PIPE, ...NAV_PERSONAL].find(
    (n) => (n.to ?? n.sub ?? '') === key,
  );
  if (!entry) return { label: '—' };
  return { label: tr(entry.zh, entry.en), to: entry.to ?? topicPath(pid, entry.sub) };
}

/**
 * 当前路径 → 面包屑层级（分组 › 侧栏条目 › 当前实体），与侧栏三组保持一致。
 * 分组段指向该组的落地页（实验室工作台 / 课题工作台 / 我的文献库）。
 * libName：文献库详情页的库名（列表缓存里已有，取不到时退回通用文案）。
 */
function crumbsFor(
  pathname: string,
  pid: string | null,
  libName?: string | null,
  labTask?: boolean,
): Crumb[] {
  const lab: Crumb = { label: tr(NAV_GROUPS.lab.zh, NAV_GROUPS.lab.en), to: '/lab' };
  const topic: Crumb = {
    label: tr(NAV_GROUPS.topic.zh, NAV_GROUPS.topic.en),
    to: pid ? topicPath(pid) : '/start',
  };
  const personal: Crumb = { label: tr(NAV_GROUPS.personal.zh, NAV_GROUPS.personal.en), to: '/library' };
  const e = (key: string) => navCrumb(key, pid);

  // 课题作用域路径统一去掉 /t/<topicId> 前缀后按同一张表匹配
  const scoped = /^\/t\/[^/]+(\/.*)?$/.exec(pathname);
  const p = scoped ? (scoped[1] ?? '/') : pathname;

  const trail = ((): Crumb[] => {
    // —— 课题研究组 ——
    if (p === '/') return [topic, e('')];
    if (p === '/research') return [topic, e('research')];
    if (p === '/forge') return [topic, e('forge')];
    if (p === '/review') return [topic, e('review')];
    if (p === '/experiment') return [topic, e('experiment')];
    if (p === '/writer') return [topic, e('writer')];
    if (p === '/paper-review') return [topic, e('paper-review')];
    if (p.startsWith('/ideas/')) return [topic, e('forge'), { label: tr('想法详情', 'Idea detail') }];
    if (p.startsWith('/experiment/')) return [topic, e('experiment'), { label: tr('实验详情', 'Experiment detail') }];
    if (p.startsWith('/writer/')) return [topic, e('writer'), { label: tr('编辑工作台', 'Editor workspace') }];
    // 任务已并入课题工作台的「任务」标签
    if (p === '/voyages') return [topic, e(''), { label: tr('任务', 'Tasks') }];
    // 任务详情按归属分流：文献库任务 / 每日新论文（以及任何不挂课题的任务）归实验室，
    // 其余归课题。归属要等任务本身取回来才知道，取不到时按课题走（占多数）。
    if (p.startsWith('/voyages/'))
      return labTask
        ? [lab, { ...e('/lab'), to: '/lab?tab=tasks' }, { label: tr('任务详情', 'Task detail') }]
        : [
            topic,
            { ...e(''), to: topicPath(pid) + '?tab=tasks' },
            { label: tr('任务详情', 'Task detail') },
          ];
    if (p === '/start') return [topic, { label: tr('选择课题', 'Pick a topic') }];
    if (p === '/projects/new') return [topic, { label: tr('新建课题', 'New topic') }];
    if (p.startsWith('/projects/')) return [topic, { label: tr('课题设置', 'Topic settings') }];
    if (p.startsWith('/join/')) return [topic, { label: tr('接受邀请', 'Accept invite') }];

    // —— 实验室组 ——
    if (p === '/lab') return [lab, e('/lab')];
    if (p === '/libraries' || p === '/wiki') return [lab, e('/libraries')];
    if (p.startsWith('/libraries/'))
      return [lab, e('/libraries'), { label: libName || tr('文献库详情', 'Library') }];
    if (p === '/daily') return [lab, e('/daily')];
    if (p.startsWith('/papers/')) return [lab, e('/libraries'), { label: tr('论文阅读', 'Paper reading') }];
    if (p.startsWith('/concepts/')) return [lab, e('/libraries'), { label: tr('概念', 'Concept') }];

    // —— 个人区 ——
    if (p === '/library') return [personal, e('/library')];
    if (p === '/skills') return [personal, e('/skills')];
    if (p === '/settings') return [personal, { label: tr('设置', 'Settings') }];
    if (p === '/admin') return [{ label: tr('管理', 'Manage') }];
    return [{ label: 'Polaris' }];
  })();

  // 最后一段是当前页：去掉链接
  return trail.map((c, i) => (i === trail.length - 1 ? { label: c.label } : c));
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
   侧边栏课题切换器（课题研究组内的特殊条目）：触发器 + 卡片式下拉菜单。
   菜单含课题列表（当前项打勾 + 蓝点）、新建课题、课题设置入口。
   折叠态只留图标按钮，菜单向右侧弹出（侧栏 z-index 已抬高，不会被主列裁剪）。
   ============================================================ */
function TopicSwitcher({ collapsed }: { collapsed: boolean }) {
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

  const triggerLabel = currentProject?.name ?? (isLoading ? tr('加载中…', 'Loading…') : tr('选择课题', 'Pick a topic'));

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
    <div ref={wrapRef} style={{ position: 'relative' }}>
      {/* 触发器：与普通导航条目同高的描边条目（图标 + 当前课题名 + 折叠箭头）；折叠态只留图标 */}
      <button
        onClick={() => setOpen((o) => !o)}
        className={'topic-switch' + (open ? ' open' : '')}
        title={currentProject ? `${tr('切换课题', 'Switch topic')} · ${currentProject.name}` : tr('切换课题', 'Switch topic')}
      >
        {/* 图标放进 24px 轨道框（nav-ic）：中心与普通条目图标同在 33px 轨道，展开/折叠不位移 */}
        <span className="nav-ic">
          <Icon name="layers" size={18} style={{ color: 'var(--accent)' }} />
        </span>
        {!collapsed && (
          <>
            <span className={'topic-switch-label' + (currentProject ? '' : ' placeholder')}>{triggerLabel}</span>
            <Icon
              name="chevDown"
              size={12}
              style={{ color: 'var(--text-3)', flexShrink: 0, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
            />
          </>
        )}
      </button>

      {/* 下拉菜单：展开态在触发器正下方；折叠态向右侧弹出盖在主列上 */}
      {open && (
        <div
          className="card"
          style={{
            position: 'absolute',
            ...(collapsed
              ? { top: 0, left: 'calc(100% + 18px)' }
              : { top: 'calc(100% + 8px)', left: 0 }),
            zIndex: 40,
            width: 280,
            padding: 6,
            boxShadow: 'var(--shadow-pop)',
            animation: 'fadeUp 0.12s ease',
          }}
        >
          <div className="mono" style={{ fontSize: 10, color: 'var(--text-4)', padding: '4px 12px 6px', letterSpacing: '0.06em' }}>
            {tr('课题', 'TOPICS')}
          </div>
          <div className="scroll" style={{ maxHeight: 320, overflowY: 'auto' }}>
            {projects.length === 0 && (
              <div style={{ padding: '10px 12px', fontSize: 12, color: 'var(--text-4)' }}>
                {isLoading ? tr('加载中…', 'Loading…') : tr('还没有课题，先创建一个', 'No topics yet — create one first')}
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
                    const scoped = /^\/t\/[^/]+(?:\/(.*))?$/.exec(location.pathname);
                    if (scoped) {
                      // 课题作用域页面：切课题保持当前子页面（如 /t/a/forge → /t/b/forge）
                      navigate(topicPath(p.id, scoped[1] || undefined));
                    } else if (/^\/projects\/(?!new)/.test(location.pathname)) {
                      // 课题设置页：跳到新课题的设置
                      navigate(`/projects/${p.id}`);
                    } else {
                      // 实体详情页或非课题域页面：回到新课题的工作台
                      navigate(topicPath(p.id));
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
            {tr('新建课题', 'New topic')}
          </button>
          {currentProjectId && (
            <button
              style={{ ...itemStyle, color: 'var(--text-2)' }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--surface-2)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              onClick={() => {
                setOpen(false);
                navigate(topicPath(currentProjectId) + '?tab=settings');
              }}
            >
              <Icon name="settings" size={13} style={{ color: 'var(--text-3)' }} />
              {tr('课题设置', 'Topic settings')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function NavItem({ n }: { n: NavEntry }) {
  const { currentProjectId } = useProject();
  // 课题作用域条目带 /t/<id> 前缀；无当前课题时退回旧路径，由重定向兜底
  const to = n.to ?? topicPath(currentProjectId, n.sub);
  // 工作台（课题根路径）与其标签项只在精确匹配时高亮，避免盖住全部子页面
  const end = n.end ?? (n.to == null && n.sub == null);
  return (
    <NavLink
      to={to}
      end={end}
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
  const navigate = useNavigate();
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

  // —— 手机：侧栏改覆盖式抽屉 ——
  // 桌面的「收起」是窄轨道（占位、可记忆），手机需要的是盖在内容上的抽屉，
  // 语义不同，所以另起一个 state；且不进 localStorage —— 每次进站默认关闭，
  // 否则一打开页面就被侧栏盖住。
  const isMobile = useIsMobile();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  // 点了导航项就关抽屉，否则新页面被侧栏盖着；转桌面宽度时也一并复位。
  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname, isMobile]);

  // —— 全局搜索（⌘K / Ctrl+K）——
  const [searchOpen, setSearchOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  // 拖到悬浮球上的论文：交给面板发起解读，取走后清空（清空由面板回调做）
  const [droppedPaperId, setDroppedPaperId] = useState<string | null>(null);
  const [droppedText, setDroppedText] = useState<string | null>(null);
  const [buddyBusy, setBuddyBusy] = useState(false);
  const [nudge, setNudge] = useState<string | null>(null);
  const [buddyDragOver, setBuddyDragOver] = useState(false);
  const [topbarMoreOpen, setTopbarMoreOpen] = useState(false);
  // 顶栏还宽裕吗。量的是**主区**：Buddy 拉开后视口没变，变窄的是主区。
  const mainRef = useRef<HTMLDivElement | null>(null);
  const [topbarRoomy, setTopbarRoomy] = useState(true);
  // 窗口塞不下「侧栏 + 够用的主区 + 停靠栏」时，Buddy 改用覆盖式（就是窄屏那套）。
  // 硬挤的结果是主区被压成竖排单字，两边都用不了——不如让一边完整。
  const [dockFits, setDockFits] = useState(() => dockMaxWidth(window.innerWidth) > 0);
  useEffect(() => {
    const onResize = () => setDockFits(dockMaxWidth(window.innerWidth) > 0);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  useEffect(() => {
    const el = mainRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setTopbarRoomy(entry.contentRect.width >= 880);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // 主动提示：今天没提过才去问一次计数——大多数次打开页面连这个请求都不会发。
  // 提示必须对应一件**真事**（跑着的实验、今天到的新论文）；为了让球看起来「活着」
  // 而常亮的红点，是在教用户忽略它。
  useEffect(() => {
    if (alreadyNudgedToday(new Date())) return;
    let alive = true;
    void api
      .getBuddyGreeting()
      .then((g) => {
        if (alive) setNudge(g.nudge ?? null);
      })
      .catch(() => {
        /* 数不出来就不提示——沉默比编一句好 */
      });
    return () => {
      alive = false;
    };
  }, []);

  const dismissNudge = useCallback(() => {
    markNudged(new Date());
    setNudge(null);
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setSearchOpen((o) => !o);
      }
      // ⌘J：PolarisBuddy。挑 J 是因为 ⌘K 已被搜索占了，而两者都该是全局的
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j') {
        e.preventDefault();
        setAssistantOpen((o) => !o);
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
        notifyDesktop(tr('新审批请求', 'New approval request'), gateTitle(msg.gate));
      } else if (msg.type === 'gate.decided') {
        void queryClient.invalidateQueries({ queryKey: ['gates'] });
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      } else if (msg.type === 'voyage.status') {
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
        void queryClient.invalidateQueries({ queryKey: ['voyage', msg.voyage_id] });
        if (msg.status === 'paused_gate') {
          toast(tr('任务等待审批', 'Task paused for approval'), 'info');
          notifyDesktop(tr('任务等待审批', 'Task paused for approval'));
        } else if (msg.status === 'done') {
          toast(tr('任务完成', 'Task done'), 'ok');
          notifyDesktop(tr('任务完成', 'Task done'));
        } else if (msg.status === 'failed') {
          toast(tr('任务失败', 'Task failed'), 'error');
          notifyDesktop(tr('任务失败', 'Task failed'));
        }
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
        if (msg.status === 'awaiting_gate') {
          toast(tr('实验等待预算审批', 'Experiment awaiting budget approval'), 'info');
          notifyDesktop(tr('实验等待预算审批', 'Experiment awaiting budget approval'));
        } else if (msg.status === 'running') {
          toast(tr('实验正式运行中', 'Experiment running'), 'info');
        } else if (msg.status === 'done') {
          toast(tr('实验完成', 'Experiment done'), 'ok');
          notifyDesktop(tr('实验完成', 'Experiment done'));
        } else if (msg.status === 'failed') {
          toast(tr('实验失败', 'Experiment failed'), 'error');
          notifyDesktop(tr('实验失败', 'Experiment failed'));
        }
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

  // 桌面端 Dock/任务栏角标 = 待审批数（web 端 setBadgeCount 是 no-op）
  useEffect(() => {
    setBadgeCount(pending.length);
  }, [pending.length]);

  // —— 面包屑（层级同侧栏）：文献库详情页多取一段库名，走详情页同一份缓存，不额外发请求 ——
  const { currentProjectId } = useProject();
  const crumbLibraryId = /^\/libraries\/([^/]+)$/.exec(location.pathname)?.[1] ?? null;
  const { data: crumbLibrary } = useQuery({
    queryKey: ['library', crumbLibraryId],
    queryFn: () => api.getLibrary(crumbLibraryId ?? ''),
    enabled: !!crumbLibraryId,
    retry: false,
    staleTime: 60_000,
  });
  // 任务详情：归属课题还是归属实验室，得看任务本身。queryKey 与详情页默认态
  // （showObsolete=false）一致，命中的是同一份缓存，正常不会多发请求。
  const crumbVoyageId = /^\/voyages\/([^/]+)$/.exec(location.pathname)?.[1] ?? null;
  const { data: crumbVoyage } = useQuery({
    queryKey: ['voyage', crumbVoyageId, false],
    queryFn: () => api.getVoyage(crumbVoyageId ?? '', { includeObsolete: false }),
    enabled: !!crumbVoyageId,
    retry: false,
    staleTime: 60_000,
  });
  const crumbs = crumbsFor(
    location.pathname,
    currentProjectId,
    crumbLibrary?.name,
    crumbVoyage ? isLabScopedTask(crumbVoyage) : false,
  );

  // —— 刷新：让所有查询失效并重取（保留页面状态，不整页重载） ——
  const [refreshing, setRefreshing] = useState(false);
  async function refreshData() {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await queryClient.invalidateQueries();
    } finally {
      setRefreshing(false);
    }
  }

  const ctx: ShellContext = { pendingGates: pending, gatesError: pendingQuery.isError, openGates };

  return (
    <div
      className={
        'app' +
        // 手机上不存在「图标轨道」形态：只有抽屉的开/关，桌面的收起态记忆保持不变，
        // 转回宽屏时自动恢复。
        (isMobile ? (mobileNavOpen ? ' nav-open' : '') : navCollapsed ? ' nav-collapsed' : '')
      }
    >
      {/* —— 侧栏（手机上是覆盖式抽屉，见 global.css 响应式一节）—— */}
      <div className="sidebar">
        <div className="sb-brand">
          <PolarisMark size={41} />
          {/* 收起后只留左侧图形标：直接不渲染字标，杜绝溢出（不靠 CSS 隐藏）。
              手机抽屉是完整宽度，字标照常显示。 */}
          {(!navCollapsed || isMobile) && <PolarisWordmark height={30} />}
        </div>
        {/* —— 实验室 + 课题研究两组（平面分组，只靠 eyebrow + 间距区分，不加分隔线）。
            放在滚动区之外：课题切换器的下拉菜单要能向右溢出到主列上 —— */}
        <div className="sb-nav-static">
          <div className="sb-section">{tr(NAV_GROUPS.lab.zh, NAV_GROUPS.lab.en)}</div>
          {NAV_LAB.map((n) => (
            <NavItem key={n.sub ?? n.to ?? 'home'} n={n} />
          ))}

          <div className="sb-section">{tr(NAV_GROUPS.topic.zh, NAV_GROUPS.topic.en)}</div>
          <TopicSwitcher collapsed={navCollapsed && !isMobile} />
          {NAV_MAIN.map((n) => (
            <NavItem key={n.sub ?? n.to ?? 'home'} n={n} />
          ))}
          {NAV_PIPE.filter((n) => {
            const key = n.sub != null ? FEATURE_BY_SUB[n.sub] : undefined;
            return key == null || me?.features?.[key] !== false;
          }).map((n) => (
            <NavItem key={n.sub ?? n.to ?? 'home'} n={n} />
          ))}
          {/* 任务 / 课题设置已并入「工作台」的标签页（概况/设置/任务），侧栏不再单列 */}
        </div>

        {/* —— 个人区：跨课题的个人页面（设置入口在底部头像菜单里，不重复占位） —— */}
        <div className="sb-scroll scroll">
          <div className="sb-section">{tr(NAV_GROUPS.personal.zh, NAV_GROUPS.personal.en)}</div>
          {NAV_PERSONAL.map((n) => (
            <NavItem key={n.to} n={n} />
          ))}
        </div>
        <div className="sb-foot">
          <UserMenu me={me} collapsed={navCollapsed && !isMobile} />
        </div>
      </div>

      {/* —— 手机抽屉的遮罩：点它关闭 —— */}
      {isMobile && mobileNavOpen && (
        <div className="sidebar-scrim" onClick={() => setMobileNavOpen(false)} />
      )}

      {/* —— 主列 —— */}
      <div className="main" ref={mainRef}>
        <div className="topbar">
          <button
            className="icon-btn nav-toggle"
            onClick={isMobile ? () => setMobileNavOpen((o) => !o) : toggleNav}
            title={
              isMobile
                ? mobileNavOpen
                  ? tr('关闭菜单', 'Close menu')
                  : tr('打开菜单', 'Open menu')
                : navCollapsed
                  ? tr('展开菜单栏', 'Expand sidebar')
                  : tr('收起菜单栏', 'Collapse sidebar')
            }
            aria-label={
              isMobile
                ? mobileNavOpen
                  ? tr('关闭菜单', 'Close menu')
                  : tr('打开菜单', 'Open menu')
                : navCollapsed
                  ? tr('展开菜单栏', 'Expand sidebar')
                  : tr('收起菜单栏', 'Collapse sidebar')
            }
            aria-expanded={isMobile ? mobileNavOpen : undefined}
          >
            <Icon name="sidebar" size={16} />
          </button>
          <div className="crumb">
            {crumbs.map((c, i) => (
              <Fragment key={`${c.label}-${i}`}>
                {i > 0 && <span className="sep">›</span>}
                {c.to ? <Link to={c.to}>{c.label}</Link> : <b>{c.label}</b>}
              </Fragment>
            ))}
          </div>
          <button
            className="icon-btn crumb-refresh"
            onClick={() => void refreshData()}
            disabled={refreshing}
            title={tr('刷新本页数据', 'Reload this page’s data')}
            aria-label={tr('刷新本页数据', 'Reload this page’s data')}
          >
            <Icon name="refresh" size={15} style={refreshing ? { animation: 'spin 1s linear infinite' } : undefined} />
          </button>
          <div className="spacer" />
          {/* 窄的时候（Buddy 一拉开就窄了）搜索框缩成一个图标：它的提示文字是奢侈品，
              而右侧那排功能入口不是。 */}
          {topbarRoomy ? (
            <div className="searchbox" role="button" tabIndex={0} onClick={() => setSearchOpen(true)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setSearchOpen(true); }}>
              <Icon name="search" size={14} />
              <span>{tr('搜索论文 / 想法 / 实验…', 'Search papers / ideas / experiments…')}</span>
              <span className="mono" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-4)' }}>⌘K</span>
            </div>
          ) : (
            <button
              className="icon-btn"
              onClick={() => setSearchOpen(true)}
              title={tr('搜索（⌘K）', 'Search (⌘K)')}
              aria-label={tr('搜索', 'Search')}
            >
              <Icon name="search" size={16} />
            </button>
          )}
          {/* PolarisBuddy：入口在顶栏右侧，不再是浮在页面上的球——球会挡住内容，
              而且没人知道它是什么。这里也是论文/选中文字的拖放落点。 */}
          <button
            className="icon-btn"
            onClick={() => setAssistantOpen((o) => !o)}
            title={tr('PolarisBuddy（⌘J）· 可把论文或选中的文字拖到这里', 'PolarisBuddy (⌘J) · drop a paper or selection here')}
            aria-label="PolarisBuddy"
            onDragOver={(e) => {
              const types = e.dataTransfer.types;
              if (types.includes(PAPER_DND_MIME) || types.includes('text/plain')) {
                e.preventDefault();
                setBuddyDragOver(true);
              }
            }}
            onDragLeave={() => setBuddyDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setBuddyDragOver(false);
              // 论文优先：拖论文行时 text/plain 里是标题，不该被当成一段要解释的话
              const paperId = e.dataTransfer.getData(PAPER_DND_MIME);
              if (paperId) {
                setDroppedPaperId(paperId);
                setAssistantOpen(true);
                return;
              }
              const text = e.dataTransfer.getData('text/plain').trim();
              if (text) {
                setDroppedText(text.slice(0, 1200));
                setAssistantOpen(true);
              }
            }}
            style={{
              position: 'relative',
              background: buddyDragOver ? 'var(--accent-soft)' : undefined,
              outline: buddyDragOver ? '1px dashed var(--accent)' : undefined,
            }}
          >
            <PolarisMark size={16} dot={!buddyBusy} />
            {/* 主动提示：顶栏按钮上一个小点；点开面板即消。没有真事时根本不出现 */}
            {nudge && (
              <span
                title={nudge}
                onClick={(e) => {
                  e.stopPropagation();
                  dismissNudge();
                  setAssistantOpen(true);
                }}
                style={{
                  position: 'absolute',
                  top: 4,
                  right: 4,
                  width: 6,
                  height: 6,
                  borderRadius: '50%',
                  background: 'var(--danger)',
                }}
              />
            )}
          </button>

          {/* 次要入口：宽时平铺，窄时收进「更多」。判据是主区实际宽度而不是视口——
              Buddy 拉开之后视口没变，变窄的是主区，按视口判会一直以为还很宽。 */}
          {topbarRoomy ? (
            <>
              {isAdmin(me) && (
                <button
                  className="icon-btn"
                  onClick={() => navigate('/admin')}
                  title={tr('管理', 'Manage')}
                  aria-label={tr('管理', 'Manage')}
                  style={location.pathname === '/admin' ? { color: 'var(--accent)', background: 'var(--surface-2)' } : undefined}
                >
                  <Icon name="settings" size={16} />
                </button>
              )}
              <LangToggle />
              <FeedbackWidget />
              <UpdateBadge />
              <button className="icon-btn" onClick={() => openGates(null)} title={tr('审批中心', 'Approvals')}>
                <Icon name="bell" size={16} />
                {pending.length > 0 && <span className="badge">{pending.length}</span>}
              </button>
            </>
          ) : (
            <div style={{ position: 'relative' }}>
              <button
                className="icon-btn"
                onClick={() => setTopbarMoreOpen((o) => !o)}
                title={tr('更多', 'More')}
                aria-label={tr('更多', 'More')}
              >
                <Icon name="sliders" size={16} />
                {pending.length > 0 && <span className="badge">{pending.length}</span>}
              </button>
              {topbarMoreOpen && (
                <>
                  {/* 点外面关掉：下拉盖着的是内容区，留着它挡路比少一次点击更糟 */}
                  <div
                    onClick={() => setTopbarMoreOpen(false)}
                    style={{ position: 'fixed', inset: 0, zIndex: 39 }}
                  />
                  <div
                    className="col gap4"
                    style={{
                      position: 'absolute',
                      right: 0,
                      top: 36,
                      zIndex: 40,
                      minWidth: 168,
                      padding: 6,
                      background: 'var(--surface)',
                      border: '0.5px solid var(--border-2)',
                      borderRadius: 10,
                      boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
                    }}
                    onClick={() => setTopbarMoreOpen(false)}
                  >
                    {isAdmin(me) && (
                      <button className="btn btn-ghost sm" style={{ justifyContent: 'flex-start' }} onClick={() => navigate('/admin')}>
                        <Icon name="settings" size={14} /> {tr('管理', 'Manage')}
                      </button>
                    )}
                    <button className="btn btn-ghost sm" style={{ justifyContent: 'flex-start' }} onClick={() => openGates(null)}>
                      <Icon name="bell" size={14} /> {tr('审批中心', 'Approvals')}
                      {pending.length > 0 && <span className="badge" style={{ position: 'static', marginLeft: 'auto' }}>{pending.length}</span>}
                    </button>
                    <div className="row gap6" style={{ padding: '2px 4px' }}>
                      <LangToggle />
                      <FeedbackWidget />
                      <UpdateBadge />
                    </div>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
        <div className="content scroll">
          <Outlet context={ctx} />
        </div>
      </div>

      {/* —— PolarisBuddy 停靠栏：它是版面的一列，内容区被挤窄而不是被盖住 ——
          浮层盖住内容，「一边看论文一边问」就只能开一下关一下；停靠栏让两边同时在场。
          窄屏不走这条（挤两列谁都看不清），下面那个覆盖式抽屉才是手机形态。 */}
      {!isMobile && dockFits && assistantOpen && (
        <BuddyDock>
          <AssistantPanel
            variant="dock"
            open
            onClose={() => setAssistantOpen(false)}
            droppedPaperId={droppedPaperId}
            onDroppedPaperHandled={() => setDroppedPaperId(null)}
            droppedText={droppedText}
            onDroppedTextHandled={() => setDroppedText(null)}
            onBusyChange={setBuddyBusy}
          />
        </BuddyDock>
      )}

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

      {(isMobile || !dockFits) && (
        <AssistantPanel
          open={assistantOpen}
          onClose={() => setAssistantOpen(false)}
          droppedPaperId={droppedPaperId}
          onDroppedPaperHandled={() => setDroppedPaperId(null)}
          droppedText={droppedText}
          onDroppedTextHandled={() => setDroppedText(null)}
          onBusyChange={setBuddyBusy}
        />
      )}

      <ToastHost />
    </div>
  );
}
