import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { Link, Outlet, useLocation, useOutletContext } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon, type IconName } from '../components/ui/Icon';
import { PolarisMark, PolarisWordmark } from '../components/ui/PolarisLogo';
import { gateTitle } from '../components/ui/GateCard';
import { ToastHost, toast } from '../components/ui/Toast';
import { UpdateBadge } from '../components/ui/UpdateBadge';
import { useAuth } from './auth';
import { topicPath, useProject } from './project';
import { ActivityDrawer, useActivity, waitingCount } from './ActivityDrawer';
import { crumbsFor, navLabel, navSectionOf, type NavKey } from './crumbs';
import { AssistantPanel } from '../features/assistant/AssistantPanel';
import { BuddyDock, dockMaxWidth } from '../features/assistant/BuddyDock';
import { alreadyNudgedToday, markNudged } from '../features/assistant/nudge';
import { PAPER_DND_MIME } from '../features/assistant/paperDrag';
import { useDirections, type Direction } from '../features/deepresearch/directions';
import { researchKeys } from '../features/deepresearch/hooks';
import { runHeadline } from '../features/deepresearch/phase';
import { SearchPalette } from './SearchPalette';
import { api, getToken, isLabScopedTask, type GateDecision, type GateRead, type ReviewMessageRead } from '../lib/api';
import { researchApi } from '../lib/researchApi';
import { tr, useLang } from '../lib/i18n';
import { LangToggle } from '../components/ui/LangToggle';
import { connectNotifications } from '../lib/ws';
import { useIsMobile } from '../lib/useBreakpoint';
import { notifyDesktop } from '../lib/desktop-notify';
import { hasHost, hostAppVersion, openExternal, setBadgeCount } from '../lib/host';

/* ============================================================
   外壳：侧栏 + 顶栏 + 任务与待办面板 + 搜索 + 助手。

   侧栏只有三件事：研究（下面列着你的课题）、论文、每日新论文；底部是设置。
   一个课题的想法 / 实验 / 写作等页面不在侧栏上，从课题页头的页签进（DirectionNav）。
   顶栏右侧：搜索 · 任务与待办 · 助手。
   ============================================================ */

/** AppShell 通过 Outlet context 暴露给子页面的能力。 */
export interface ShellContext {
  /** 待处理的审批（真实 API）。 */
  pendingGates: GateRead[];
  /** 审批列表是否加载失败（引擎没起来）。 */
  gatesError: boolean;
  /** 打开「任务与待办」面板，可选展开某条审批。 */
  openGates: (gateId?: string | null) => void;
}

export function useShell(): ShellContext {
  return useOutletContext<ShellContext>();
}

/** 侧栏最多列几个课题；更多的在「研究」首页。 */
const SIDEBAR_TOPICS = 7;

const NAV_ICONS: Record<Exclude<NavKey, 'activity'>, IconName> = {
  research: 'compass',
  papers: 'book',
  daily: 'sparkle',
  settings: 'settings',
};

function SideItem({ to, navKey, active }: { to: string; navKey: Exclude<NavKey, 'activity'>; active: boolean }) {
  const label = navLabel(navKey);
  return (
    <Link to={to} className={'nav-item' + (active ? ' active' : '')} title={label} aria-current={active ? 'page' : undefined}>
      <span className="nav-ic">
        <Icon name={NAV_ICONS[navKey]} size={18} />
      </span>
      <span className="nav-label" style={{ flex: 1 }}>
        {label}
      </span>
    </Link>
  );
}

/** 侧栏里「研究」下面的课题：一个状态点 + 名字。当前课题高亮。 */
function TopicList({ directions, activeTopicId }: { directions: Direction[]; activeTopicId: string | null }) {
  if (directions.length === 0) return null;
  // 当前课题排不进前几个时也要露出来，否则侧栏上找不到「我在哪」
  const top = directions.slice(0, SIDEBAR_TOPICS);
  const current = activeTopicId ? directions.find((d) => d.projectId === activeTopicId) : undefined;
  const shown = current && !top.includes(current) ? [...top.slice(0, SIDEBAR_TOPICS - 1), current] : top;
  const rest = directions.length - shown.length;
  return (
    <div className="sb-topics" role="list" aria-label={tr('课题', 'Topics')}>
      {shown.map((d) => {
        const head = d.run ? runHeadline(d.run) : null;
        const active = d.projectId === activeTopicId;
        return (
          <Link
            key={d.projectId}
            role="listitem"
            to={d.href}
            className={'sb-topic' + (active ? ' active' : '')}
            title={head ? `${d.name} · ${head.label}` : d.name}
            aria-current={active ? 'true' : undefined}
          >
            <span className={'sb-topic-dot' + (head ? ` is-${head.tone}` : '')} aria-hidden="true" />
            <span className="sb-topic-name">{d.name}</span>
          </Link>
        );
      })}
      {rest > 0 && (
        <Link to="/research" className="sb-topic sb-topic-more">
          <span className="sb-topic-dot" aria-hidden="true" style={{ visibility: 'hidden' }} />
          <span className="sb-topic-name">{tr(`全部 ${directions.length} 个课题`, `All ${directions.length} topics`)}</span>
        </Link>
      )}
    </div>
  );
}

/* ============================================================
   反馈入口 —— 直接打开 GitHub new-issue 页（#617）。
   仓库地址与 desktop/src/main/menu.ts 的同名常量保持一致——前端同样
   写死，不为一个链接引入后端配置依赖。
   ============================================================ */
const REPO_URL = 'https://github.com/ZJU-REAL/Polaris';

function FeedbackLink() {
  const location = useLocation();
  // 版本号取 /api/health；预取并缓存，点击时同步取用，
  // 不在点击回调里 await——经过 await 再 window.open 会被浏览器当弹窗拦截。
  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: () => api.health(),
    staleTime: Infinity,
    retry: false,
  });

  const openIssuePage = () => {
    const desktop = hasHost();
    const platform = desktop
      ? `desktop ${hostAppVersion() ?? '?'} / ${navigator.userAgent}`
      : `browser / ${navigator.userAgent}`;
    const body = [
      tr('## 问题描述', '## What happened'),
      '',
      tr(
        '（描述问题或建议；截图可以直接粘贴到这里）',
        '(Describe the problem or suggestion; screenshots can be pasted right here.)'
      ),
      '',
      tr('## 复现步骤', '## Steps to reproduce'),
      '',
      '1. ',
      '',
      tr('## 期望结果', '## Expected behavior'),
      '',
      '',
      '---',
      `${tr('版本', 'Version')}: ${health?.version ?? 'unknown'}`,
      `${tr('平台', 'Platform')}: ${platform}`,
      `${tr('页面', 'Route')}: ${location.pathname}`,
    ].join('\n');
    const url = `${REPO_URL}/issues/new?body=${encodeURIComponent(body)}`;
    // 桌面端交给系统浏览器（GitHub 登录态在那边）；web 端新标签页打开
    if (desktop) void openExternal(url);
    else window.open(url, '_blank', 'noopener');
  };

  return (
    <button
      className="icon-btn"
      onClick={openIssuePage}
      title={tr('反馈', 'Feedback')}
      aria-label={tr('反馈', 'Feedback')}
    >
      <Icon name="chat" size={16} />
    </button>
  );
}

export function AppShell() {
  // 壳层自己订阅语言：它的文案就地重渲染即可，侧栏状态、助手里正在跑的对话都留着。
  // 路由出口下面那一层则要真重挂载，见下面 <Fragment key={lang}>。
  const lang = useLang();
  const location = useLocation();
  const queryClient = useQueryClient();

  // —— 任务与待办面板 ——
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
  // 语义不同，所以另起一个 state；且不进 localStorage —— 每次进站默认关闭。
  const isMobile = useIsMobile();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  // 点了导航项就关抽屉，否则新页面被侧栏盖着；转桌面宽度时也一并复位。
  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname, isMobile]);

  // —— 全局搜索（⌘K / Ctrl+K）——
  const [searchOpen, setSearchOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  // 拖到助手按钮上的论文：交给面板发起解读，取走后清空（清空由面板回调做）
  const [droppedPaperId, setDroppedPaperId] = useState<string | null>(null);
  const [droppedText, setDroppedText] = useState<string | null>(null);
  const [buddyBusy, setBuddyBusy] = useState(false);
  const [nudge, setNudge] = useState<string | null>(null);
  const [buddyDragOver, setBuddyDragOver] = useState(false);
  // 顶栏还宽裕吗。量的是**主区**：助手拉开后视口没变，变窄的是主区。
  const mainRef = useRef<HTMLDivElement | null>(null);
  const [topbarRoomy, setTopbarRoomy] = useState(true);
  // 窗口塞不下「侧栏 + 够用的主区 + 停靠栏」时，助手改用覆盖式（就是窄屏那套）。
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
  // 提示必须对应一件**真事**（跑着的实验、今天到的新论文）。
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
      // ⌘J：助手。挑 J 是因为 ⌘K 已被搜索占了，而两者都该是全局的
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j') {
        e.preventDefault();
        setAssistantOpen((o) => !o);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  // —— 审批（真实 API，引擎没起来时降级为空列表 + 提示） ——
  const pendingQuery = useQuery({
    queryKey: ['gates', 'pending'],
    queryFn: () => api.listGates('pending'),
    retry: false,
    refetchInterval: 60_000,
  });
  const pending = pendingQuery.data ?? [];

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
      toast(`${tr('审批未提交，请重试', 'Couldn’t submit the approval. Try again.')} ${err instanceof Error ? err.message : String(err)}`, 'error');
    },
  });

  function openGates(gateId?: string | null) {
    setExpandedGate(gateId ?? null);
    setDrawerOpen(true);
  }
  function decide(id: string, decision: GateDecision, comment?: string) {
    decideMutation.mutate({ id, decision, comment });
  }

  // —— WebSocket 通知：审批 / 任务事件 → invalidate + toast ——
  const { token } = useAuth();
  // leaderboard 缓存键带课题 id（['leaderboard', pid]），失效要按同键构造。
  // 消息本身不带课题 id，取当前课题；经 ref 读取是为了不把 currentProjectId
  // 放进下面 effect 的依赖里——那会在每次切换课题时重连整条 WebSocket。
  const { currentProjectId, projects } = useProject();
  const currentProjectIdRef = useRef(currentProjectId);
  useEffect(() => {
    currentProjectIdRef.current = currentProjectId;
  }, [currentProjectId]);
  useEffect(() => {
    if (!token) return;
    const close = connectNotifications(getToken, (msg) => {
      if (msg.type === 'gate.created') {
        void queryClient.invalidateQueries({ queryKey: ['gates'] });
        toast(`${tr('新的审批', 'New approval')}：${gateTitle(msg.gate)}`, 'info');
        notifyDesktop(tr('新的审批', 'New approval'), gateTitle(msg.gate));
      } else if (msg.type === 'gate.decided') {
        void queryClient.invalidateQueries({ queryKey: ['gates'] });
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      } else if (msg.type === 'voyage.status') {
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
        void queryClient.invalidateQueries({ queryKey: ['voyage', msg.voyage_id] });
        // 一次研究的阶段就是任务：任务状态一变，课题列表上的状态点跟着取一次
        void queryClient.invalidateQueries({ queryKey: researchKeys.list });
        if (msg.status === 'paused_gate') {
          toast(tr('任务等待审批', 'Task paused for approval'), 'info');
          notifyDesktop(tr('任务等待审批', 'Task paused for approval'));
        } else if (msg.status === 'done') {
          toast(tr('任务已完成', 'Task finished'), 'ok');
          notifyDesktop(tr('任务已完成', 'Task finished'));
        } else if (msg.status === 'failed') {
          toast(tr('任务失败', 'Task failed'), 'error');
          notifyDesktop(tr('任务失败', 'Task failed'));
        }
      } else if (msg.type === 'voyage.ask') {
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
        void queryClient.invalidateQueries({ queryKey: ['voyage', msg.voyage_id] });
        void queryClient.invalidateQueries({ queryKey: ['voyage-messages', msg.voyage_id] });
        void queryClient.invalidateQueries({ queryKey: researchKeys.list });
        toast(`${tr('任务需要你回答', 'A task needs your answer')}：${msg.question}`, 'info');
        notifyDesktop(tr('任务需要你回答', 'A task needs your answer'), msg.question);
      } else if (msg.type === 'review.message') {
        // 正在看该 session 的组件共享此 query cache → 直接乐观追加（按 id 去重）
        queryClient.setQueryData<ReviewMessageRead[]>(['session-messages', msg.session_id], (old) =>
          old === undefined ? undefined : old.some((m) => m.id === msg.message.id) ? old : [...old, msg.message],
        );
        void queryClient.invalidateQueries({ queryKey: ['idea-sessions'] });
      } else if (msg.type === 'idea.status') {
        void queryClient.invalidateQueries({ queryKey: ['ideas'] });
        void queryClient.invalidateQueries({ queryKey: ['idea', msg.idea_id] });
        void queryClient.invalidateQueries({ queryKey: ['leaderboard', currentProjectIdRef.current] });
        void queryClient.invalidateQueries({ queryKey: ['forge-state'] });
      } else if (msg.type === 'manuscript.status') {
        // 写作页靠这里实时刷新（起草 writing→compiled 等流转），不再快轮询
        void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
        void queryClient.invalidateQueries({ queryKey: ['manuscript', msg.manuscript_id] });
        // 起草结束（离开 writing 态）→ 收起 AI 光标/状态条
        if (msg.status !== 'writing') {
          queryClient.setQueryData(['ai-writing', msg.manuscript_id], null);
        }
      } else if (msg.type === 'manuscript.ai_writing') {
        // AI 起草相位 → 写入本地缓存，写作页据此画 AI 光标与状态条（无网络请求）。
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
        } else if (msg.status === 'waiting_user') {
          toast(tr('实验已暂停，需要你回答', 'Experiment paused for your answer'), 'info');
          notifyDesktop(tr('实验已暂停，需要你回答', 'Experiment paused for your answer'));
        } else if (msg.status === 'running') {
          toast(tr('实验开始运行', 'Experiment started'), 'info');
        } else if (msg.status === 'done') {
          toast(tr('实验已完成', 'Experiment finished'), 'ok');
          notifyDesktop(tr('实验已完成', 'Experiment finished'));
        } else if (msg.status === 'failed') {
          toast(tr('实验失败', 'Experiment failed'), 'error');
          notifyDesktop(tr('实验失败', 'Experiment failed'));
        }
      }
    });
    return close;
  }, [token, queryClient]);

  // —— 课题列表（侧栏）与等你处理的数量（顶栏） ——
  const { directions, byProject, byLibrary } = useDirections();
  const { data: activity } = useActivity();
  const waiting = waitingCount(activity, pending.length);

  // 桌面端 Dock/任务栏角标 = 等你处理的数量（web 端 setBadgeCount 是 no-op）
  useEffect(() => {
    setBadgeCount(waiting);
  }, [waiting]);

  // —— 面包屑：文献库详情页多取一段库名，走详情页同一份缓存，不额外发请求 ——
  const crumbLibraryId = /^\/libraries\/([^/]+)$/.exec(location.pathname)?.[1] ?? null;
  const { data: crumbLibrary } = useQuery({
    queryKey: ['library', crumbLibraryId],
    queryFn: () => api.getLibrary(crumbLibraryId ?? ''),
    enabled: !!crumbLibraryId,
    retry: false,
    staleTime: 60_000,
  });
  // 任务详情：属于哪个课题得看任务本身。queryKey 与详情页默认态一致，命中同一份缓存。
  const crumbVoyageId = /^\/voyages\/([^/]+)$/.exec(location.pathname)?.[1] ?? null;
  const { data: crumbVoyage } = useQuery({
    queryKey: ['voyage', crumbVoyageId, false],
    queryFn: () => api.getVoyage(crumbVoyageId ?? '', { includeObsolete: false }),
    enabled: !!crumbVoyageId,
    retry: false,
    staleTime: 60_000,
  });
  // 研究时间线：课题是哪个要看这次研究，与页面用同一份缓存
  const crumbResearchId = /^\/research\/([^/]+)$/.exec(location.pathname)?.[1] ?? null;
  const { data: crumbResearch } = useQuery({
    queryKey: ['research', crumbResearchId],
    queryFn: () => researchApi.getRun(crumbResearchId ?? ''),
    enabled: !!crumbResearchId,
    retry: false,
    staleTime: 60_000,
  });

  // 这一页属于哪个课题：课题页看 URL，时间线看那次研究，任务看任务，其余详情页看当前课题
  const urlTopicId = /^\/t\/([^/]+)/.exec(location.pathname)?.[1] ?? null;
  const voyageTopic = crumbVoyage
    ? (byLibrary(crumbVoyage.library_id) ?? byProject(crumbVoyage.project_id))
    : null;
  const taskOutsideTopic = !!crumbVoyage && !voyageTopic && isLabScopedTask(crumbVoyage);
  const section = navSectionOf(location.pathname, taskOutsideTopic);
  let activeTopicId: string | null = null;
  if (crumbResearchId) activeTopicId = crumbResearch?.project_id ?? null;
  else if (urlTopicId) activeTopicId = urlTopicId;
  else if (crumbVoyageId) activeTopicId = voyageTopic?.projectId ?? null;
  else if (/^\/(ideas|experiment|writer)\//.test(location.pathname)) activeTopicId = currentProjectId;
  const activeTopic = byProject(activeTopicId);
  const topicName =
    activeTopic?.name ?? crumbResearch?.name ?? projects.find((p) => p.id === activeTopicId)?.name ?? '';
  const crumbs = crumbsFor(location.pathname, {
    topic: activeTopicId
      ? { id: activeTopicId, name: topicName, href: activeTopic?.href ?? topicPath(activeTopicId) }
      : null,
    libraryName: crumbLibrary?.name,
    taskOutsideTopic,
  });

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
  const collapsed = navCollapsed && !isMobile;
  const activityLabel = navLabel('activity');
  const activityTitle =
    waiting > 0 ? tr(`${activityLabel} · ${waiting} 件事等你处理`, `${activityLabel} · ${waiting} waiting for you`) : activityLabel;

  return (
    <div
      className={
        'app' +
        // 手机上不存在「图标轨道」形态：只有抽屉的开/关，桌面的收起态记忆保持不变。
        (isMobile ? (mobileNavOpen ? ' nav-open' : '') : navCollapsed ? ' nav-collapsed' : '')
      }
    >
      {/* —— 侧栏（手机上是覆盖式抽屉，见 global.css 响应式一节）—— */}
      <div className="sidebar">
        <div className="sb-brand">
          <PolarisMark size={41} />
          {/* 收起后只留左侧图形标：直接不渲染字标，杜绝溢出（不靠 CSS 隐藏）。 */}
          {!collapsed && <PolarisWordmark height={30} />}
        </div>
        <nav className="sb-nav scroll" aria-label={tr('主导航', 'Main')}>
          <SideItem to="/research" navKey="research" active={section === 'research' && !activeTopicId} />
          {!collapsed && <TopicList directions={directions} activeTopicId={activeTopicId} />}
          <SideItem to="/libraries" navKey="papers" active={section === 'papers'} />
          <SideItem to="/daily" navKey="daily" active={section === 'daily'} />
        </nav>
        <div className="sb-foot">
          <SideItem to="/settings" navKey="settings" active={section === 'settings'} />
          {!collapsed && (
            <div className="sb-tools">
              <LangToggle />
              <FeedbackLink />
              <UpdateBadge />
            </div>
          )}
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
                  ? tr('展开侧栏', 'Expand sidebar')
                  : tr('收起侧栏', 'Collapse sidebar')
            }
            aria-label={
              isMobile
                ? mobileNavOpen
                  ? tr('关闭菜单', 'Close menu')
                  : tr('打开菜单', 'Open menu')
                : navCollapsed
                  ? tr('展开侧栏', 'Expand sidebar')
                  : tr('收起侧栏', 'Collapse sidebar')
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
            title={tr('刷新', 'Refresh')}
            aria-label={tr('刷新', 'Refresh')}
          >
            <Icon name="refresh" size={15} style={refreshing ? { animation: 'spin 1s linear infinite' } : undefined} />
          </button>
          <div className="spacer" />
          {/* 顶栏右侧只有三样：搜索 · 任务与待办 · 助手。窄的时候（助手一拉开就窄了）各自缩成图标。 */}
          {topbarRoomy ? (
            <div className="searchbox" role="button" tabIndex={0} onClick={() => setSearchOpen(true)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setSearchOpen(true); }}>
              <Icon name="search" size={14} />
              <span>{tr('搜索论文、想法、实验…', 'Search papers, ideas, experiments…')}</span>
              <span className="mono" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-3)' }}>⌘K</span>
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
          {topbarRoomy ? (
            <button
              className="btn btn-ghost sm act-trigger"
              data-activity-trigger
              onClick={() => openGates(null)}
              title={activityTitle}
            >
              <Icon name="bell" size={14} />
              {activityLabel}
              {waiting > 0 && <span className="act-count">{waiting}</span>}
            </button>
          ) : (
            <button
              className="icon-btn"
              data-activity-trigger
              onClick={() => openGates(null)}
              title={activityTitle}
              aria-label={activityTitle}
            >
              <Icon name="bell" size={16} />
              {waiting > 0 && <span className="badge">{waiting}</span>}
            </button>
          )}
          {/* 助手：也是论文 / 选中文字的拖放落点。 */}
          <button
            className="icon-btn"
            onClick={() => setAssistantOpen((o) => !o)}
            title={tr('助手（⌘J）· 可拖入论文或选中的文字', 'Assistant (⌘J) · Drop a paper or text here')}
            aria-label={tr('助手', 'Assistant')}
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
            {/* 主动提示：按钮上一个小点；点开面板即消。没有真事时根本不出现 */}
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
        </div>
        <div className="content scroll">
          {/* 业务页按语言重挂载：这些页面的文案埋在深层子树里，而路由表给出的元素是稳定
              引用，父组件重渲染带不动它们——换 key 是这里唯一可靠的重新求值方式。 */}
          <Fragment key={lang}>
            <Outlet context={ctx} />
          </Fragment>
        </div>
      </div>

      {/* —— 助手停靠栏：它是版面的一列，内容区被挤窄而不是被盖住 —— */}
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

      <ActivityDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        pendingGates={pending}
        gatesError={pendingQuery.isError}
        onRetryGates={() => void pendingQuery.refetch()}
        expandedGate={expandedGate}
        onToggleGate={(id) => setExpandedGate(expandedGate === id ? null : id)}
        onDecide={decide}
        deciding={decideMutation.isPending}
      />

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
