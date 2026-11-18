import { Suspense, lazy, useEffect, type ComponentType, type ReactNode } from 'react';
import {
  Navigate,
  Outlet,
  createBrowserRouter,
  useLocation,
  useOutletContext,
  useParams,
  useSearchParams,
} from 'react-router-dom';
import { AppShell } from './AppShell';
import { RequireAuth } from './auth';
import { DirectionHeader } from './DirectionNav';
import { ProjectProvider, topicPath, useProject } from './project';
import { useDirections } from '../features/deepresearch/directions';

/* ============================================================
   路由表。结构跟着侧栏走：

   研究      /research                 首页：一个输入框 + 课题列表
             /research/:id             有研究进展的课题的概览（时间线）
             /t/:topicId               没有研究进展的课题的概览；有的话转到时间线
             /t/:topicId/<页签>        forge 想法 · experiment 实验 · writer 写作 · review 想法评审 ·
                                       paper-review 稿件评审 · research 相关研究 · tasks 任务 · settings 设置
             /ideas/:id /experiment/:id /writer/:id /voyages/:id   具体条目
   论文      /libraries /libraries/:id /library /papers/:id/read /concepts/:id
   每日新论文 /daily
   任务与待办 /activity                 全部任务（顶栏面板里的「查看全部任务」）
   设置      /settings

   旧地址都还能用，见最下面「旧地址」一节。
   ============================================================ */

// 路由级代码分割：每个页面独立 chunk，首屏只加载壳层与当前页
function page<K extends string>(
  loader: () => Promise<Record<K, ComponentType>>,
  name: K,
): ReactNode {
  const Page = lazy(async () => {
    const mod = await loader();
    return { default: mod[name] as ComponentType };
  });
  return (
    <Suspense fallback={<PageFallback />}>
      <Page />
    </Suspense>
  );
}

function PageFallback() {
  return (
    <div className="col gap16" style={{ padding: 28 }}>
      <div className="skel" style={{ width: 220, height: 28 }} />
      <div className="skel" style={{ width: '55%', height: 14 }} />
      <div className="skel" style={{ width: '100%', height: 180 }} />
      <div className="skel" style={{ width: '100%', height: 120 }} />
    </div>
  );
}

/**
 * 具体条目页（想法 / 实验 / 稿件 / 任务详情）的守卫：一个课题都没有时回「研究」首页。
 * 加载中渲染骨架（不闪跳）；加载失败（引擎连不上）放行，由页面自己降级。
 * 透传 AppShell 的 Outlet context，子页面 useShell() 不受影响。
 */
function RequireTopic() {
  const { projects, isLoading, isError } = useProject();
  const ctx = useOutletContext();
  if (isLoading) return <PageFallback />;
  if (!isError && projects.length === 0) return <Navigate to="/research" replace />;
  return <Outlet context={ctx} />;
}

/**
 * /t/:topicId 布局：URL 是「当前课题」的事实源，并给这个课题的每一页套上同一个页头
 * （课题名 + 页签）。
 * - URL 中的课题在列表里 → 同步给 ProjectProvider，子页面照常用 useProject().currentProjectId；
 * - 列表加载完但课题不存在 → 回「研究」首页；
 * - 列表加载中 / context 尚未同步 → 骨架（不闪现上一个课题的数据）；
 * - 列表加载失败（引擎连不上）→ 信任 URL 放行，页面自己降级。
 */
function TopicScope() {
  const { topicId = '' } = useParams();
  const { projects, isLoading, isError, currentProjectId, setCurrentProjectId } = useProject();
  const ctx = useOutletContext();
  const known = projects.find((p) => p.id === topicId);

  useEffect(() => {
    if (topicId && (known || isError) && topicId !== currentProjectId) setCurrentProjectId(topicId);
  }, [topicId, known, isError, currentProjectId, setCurrentProjectId]);

  if (isLoading) return <PageFallback />;
  if (!known && !isError) return <Navigate to="/research" replace />;
  if (topicId !== currentProjectId) return <PageFallback />;
  return (
    <div className="topic-layout">
      <div className="topic-shell">
        <DirectionHeader projectId={topicId} fallbackName={known?.name} />
      </div>
      <Outlet context={ctx} />
    </div>
  );
}

/**
 * /t/:topicId 本身：课题的概览。
 * - 有研究进展 → 概览就是时间线（/research/:runId）；
 * - 没有 → 课题概览页（现有的内容 + 「用这个方向开始研究」）。
 * 旧的 `?tab=settings` / `?tab=tasks`（原来是工作台里的标签）转到各自的页面。
 */
function TopicHome() {
  const { topicId = '' } = useParams();
  const [searchParams] = useSearchParams();
  const { byProject, isLoading } = useDirections();
  const tab = searchParams.get('tab');
  if (tab === 'settings' || tab === 'tasks') return <Navigate to={topicPath(topicId, tab)} replace />;
  if (isLoading) return <PageFallback />;
  const run = byProject(topicId)?.run;
  if (run) return <Navigate to={`/research/${run.id}`} replace />;
  return <>{topicOverview}</>;
}

// 元素建一次：page() 每次调用都会造一个新的 lazy 组件，放在渲染里会反复重挂载
const topicOverview = page(() => import('../features/dashboard/TopicOverviewPage'), 'TopicOverviewPage');

/* —————————————————— 旧地址 —————————————————— */

/**
 * 不带课题的旧路径（/forge、/review…）→ 当前课题下的同名页面。应用内部在「还不知道是
 * 哪个课题」时也会落到这里（topicPath(null, sub)）。
 * 记住的课题（仍在列表中）优先，否则列表第一个；都没有 → 「研究」首页。
 */
function LegacyTopicRedirect({ sub }: { sub?: string }) {
  const { projects, isLoading, isError, currentProjectId } = useProject();
  const location = useLocation();
  if (isLoading) return <PageFallback />;
  const remembered = currentProjectId && projects.some((p) => p.id === currentProjectId) ? currentProjectId : null;
  const id = remembered ?? projects[0]?.id ?? (isError ? currentProjectId : null);
  if (!id) return <Navigate to="/research" replace />;
  return <Navigate to={topicPath(id, sub) + location.search + location.hash} replace />;
}

/** 旧「管理」页 `/admin` → `/settings`（标签名两边一致，query 原样带过去）。 */
function AdminRedirect() {
  const location = useLocation();
  return <Navigate to={'/settings' + location.search + location.hash} replace />;
}

/** 旧课题设置页 `/projects/:id` → `/t/:id/settings`。 */
function ProjectSettingsRedirect() {
  const { id = '' } = useParams();
  return <Navigate to={topicPath(id, 'settings')} replace />;
}

export const router = createBrowserRouter([
  {
    path: '/',
    element: (
      <RequireAuth>
        <ProjectProvider>
          <AppShell />
        </ProjectProvider>
      </RequireAuth>
    ),
    children: [
      // —— 研究 ——
      { index: true, element: <Navigate to="/research" replace /> },
      { path: 'research', element: page(() => import('../features/deepresearch/ResearchHomePage'), 'ResearchHomePage') },
      { path: 'research/:id', element: page(() => import('../features/deepresearch/ResearchRunPage'), 'ResearchRunPage') },
      {
        element: <RequireTopic />,
        children: [
          // 课题的各页：/t/:topicId 前缀，URL 即当前课题
          {
            path: 't/:topicId',
            element: <TopicScope />,
            children: [
              { index: true, element: <TopicHome /> },
              { path: 'forge', element: page(() => import('../features/forge/ForgePage'), 'ForgePage') },
              { path: 'review', element: page(() => import('../features/review/ReviewPage'), 'ReviewPage') },
              { path: 'experiment', element: page(() => import('../features/experiment/ExperimentPage'), 'ExperimentPage') },
              { path: 'writer', element: page(() => import('../features/writer/WriterPage'), 'WriterPage') },
              { path: 'paper-review', element: page(() => import('../features/paper-review/PaperReviewPage'), 'PaperReviewPage') },
              { path: 'research', element: page(() => import('../features/research/ResearchPage'), 'ResearchPage') },
              { path: 'tasks', element: page(() => import('../features/dashboard/TopicPages'), 'TopicTasksPage') },
              { path: 'settings', element: page(() => import('../features/dashboard/TopicPages'), 'TopicSettingsPage') },
              // 旧地址：课题的任务列表、课题名下的文献页
              { path: 'voyages', element: <Navigate to="../tasks" replace /> },
              { path: 'wiki', element: page(() => import('../features/libraries/TopicWikiRedirect'), 'TopicWikiRedirect') },
            ],
          },
          // 具体条目：按条目 id 取数，保持顶层路径
          { path: 'voyages/:id', element: page(() => import('../features/voyages/VoyageDetailPage'), 'VoyageDetailPage') },
          { path: 'ideas/:id', element: page(() => import('../features/forge/IdeaDetailPage'), 'IdeaDetailPage') },
          { path: 'experiment/:id', element: page(() => import('../features/experiment/ExperimentDetailPage'), 'ExperimentDetailPage') },
          { path: 'writer/:id', element: page(() => import('../features/writer/WriterEditorPage'), 'WriterEditorPage') },
          // 旧地址：不带课题的路径 → 当前课题下的同名页面
          { path: 'wiki', element: <LegacyTopicRedirect sub="wiki" /> },
          { path: 'forge', element: <LegacyTopicRedirect sub="forge" /> },
          { path: 'review', element: <LegacyTopicRedirect sub="review" /> },
          { path: 'experiment', element: <LegacyTopicRedirect sub="experiment" /> },
          { path: 'writer', element: <LegacyTopicRedirect sub="writer" /> },
          { path: 'paper-review', element: <LegacyTopicRedirect sub="paper-review" /> },
          { path: 'voyages', element: <LegacyTopicRedirect sub="tasks" /> },
        ],
      },

      // —— 论文 ——（按 id 取数，和课题无关：没有任何课题的人也能读论文）
      { path: 'libraries', element: page(() => import('../features/libraries/LibrariesPage'), 'LibrariesPage') },
      { path: 'libraries/:id', element: page(() => import('../features/libraries/LibraryDetailPage'), 'LibraryDetailPage') },
      { path: 'library', element: page(() => import('../features/library/LibraryPage'), 'LibraryPage') },
      { path: 'papers/:id/read', element: page(() => import('../features/reading/ReadingPage'), 'ReadingPage') },
      // `?library=` 可选，带上就只列该库里关联的论文（从库的上下文点进来时带）
      { path: 'concepts/:id', element: page(() => import('../features/wiki/ConceptPage'), 'ConceptPage') },

      // —— 每日新论文 ——
      { path: 'daily', element: page(() => import('../features/daily/DailyPage'), 'DailyPage') },

      // —— 任务与待办：全部任务 ——
      { path: 'activity', element: page(() => import('../features/library-tasks/LibraryTasksPage'), 'ActivityPage') },

      // —— 设置 ——
      { path: 'settings', element: page(() => import('../features/settings/SettingsPage'), 'SettingsPage') },

      // —— 旧地址 ——
      { path: 'start', element: <Navigate to="/research" replace /> },
      // 旧的新建课题向导：课题现在从「研究」首页的一句方向开始
      { path: 'projects/new', element: <Navigate to="/research" replace /> },
      { path: 'lab', element: <Navigate to="/activity" replace /> },
      { path: 'projects/:id', element: <ProjectSettingsRedirect /> },
      { path: 'mcp-tools', element: <Navigate to="/settings?tab=mcp" replace /> },
      { path: 'admin', element: <AdminRedirect /> },
    ],
  },
]);
