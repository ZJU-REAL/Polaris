import { Suspense, lazy, useEffect, type ComponentType, type ReactNode } from 'react';
import { Navigate, Outlet, createBrowserRouter, useLocation, useOutletContext, useParams } from 'react-router-dom';
import { AppShell } from './AppShell';
import { RequireAuth } from './auth';
import { ProjectProvider, topicPath, useProject } from './project';

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
 * 课题作用域路由守卫：确认没有任何课题时统一重定向到 /start，
 * 替代各页面重复的「还没有课题」空态。加载中渲染骨架（不闪跳）；
 * 加载失败（后端不可用）放行，由页面自身优雅降级。
 * 透传 AppShell 的 Outlet context，子页面 useShell() 不受影响。
 */
function RequireTopic() {
  const { projects, isLoading, isError } = useProject();
  const ctx = useOutletContext();
  if (isLoading) return <PageFallback />;
  if (!isError && projects.length === 0) return <Navigate to="/start" replace />;
  return <Outlet context={ctx} />;
}

/**
 * /t/:topicId 布局路由：URL 是课题作用域的事实源。
 * - URL 中的课题在列表里 → 同步给 ProjectProvider（含写 localStorage），子页面照常用
 *   useProject().currentProjectId 拉数据；
 * - 列表加载完但 URL 课题不存在 → 重定向 /start；
 * - 列表加载中 / context 尚未同步 → 渲染骨架（不闪现上一个课题的数据）；
 * - 列表加载失败（后端不可用）→ 信任 URL 放行，页面自身降级。
 */
function TopicScope() {
  const { topicId = '' } = useParams();
  const { projects, isLoading, isError, currentProjectId, setCurrentProjectId } = useProject();
  const ctx = useOutletContext();
  const known = projects.some((p) => p.id === topicId);

  useEffect(() => {
    if (topicId && (known || isError) && topicId !== currentProjectId) setCurrentProjectId(topicId);
  }, [topicId, known, isError, currentProjectId, setCurrentProjectId]);

  if (isLoading) return <PageFallback />;
  if (!known && !isError) return <Navigate to="/start" replace />;
  if (topicId !== currentProjectId) return <PageFallback />;
  return <Outlet context={ctx} />;
}

/**
 * 旧课题域路径（/、/wiki、/forge…）重定向：收藏夹 / 刷新兼容。
 * localStorage 记住的课题（仍在列表中）优先，否则列表第一个；都没有 → /start。
 * 列表加载失败时信任 localStorage 里的 id（页面自身降级）。
 */
function LegacyTopicRedirect({ sub }: { sub?: string }) {
  const { projects, isLoading, isError, currentProjectId } = useProject();
  const location = useLocation();
  if (isLoading) return <PageFallback />;
  const remembered = currentProjectId && projects.some((p) => p.id === currentProjectId) ? currentProjectId : null;
  const id = remembered ?? projects[0]?.id ?? (isError ? currentProjectId : null);
  if (!id) return <Navigate to="/start" replace />;
  // 保留查询串与锚点（如 /wiki?paper=xxx）
  return <Navigate to={topicPath(id, sub) + location.search + location.hash} replace />;
}

export const router = createBrowserRouter([
  { path: '/login', element: page(() => import('../features/auth/LoginPage'), 'LoginPage') },
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
      // —— 课题作用域路由：没有任何课题时统一重定向到 /start ——
      {
        element: <RequireTopic />,
        children: [
          // 课题域列表页：/t/:topicId 前缀，URL 即作用域
          {
            path: 't/:topicId',
            element: <TopicScope />,
            children: [
              { index: true, element: page(() => import('../features/dashboard/DashboardPage'), 'DashboardPage') },
              // 旧文献追踪路径 → 该课题隐式库的 /libraries/:id（保留 ?paper= 等深链）
              { path: 'wiki', element: page(() => import('../features/libraries/TopicWikiRedirect'), 'TopicWikiRedirect') },
              { path: 'research', element: page(() => import('../features/research/ResearchPage'), 'ResearchPage') },
              { path: 'forge', element: page(() => import('../features/forge/ForgePage'), 'ForgePage') },
              { path: 'review', element: page(() => import('../features/review/ReviewPage'), 'ReviewPage') },
              { path: 'experiment', element: page(() => import('../features/experiment/ExperimentPage'), 'ExperimentPage') },
              { path: 'writer', element: page(() => import('../features/writer/WriterPage'), 'WriterPage') },
              { path: 'paper-review', element: page(() => import('../features/paper-review/PaperReviewPage'), 'PaperReviewPage') },
              { path: 'voyages', element: page(() => import('../features/voyages/VoyagesPage'), 'VoyagesPage') },
            ],
          },
          // 实体详情页：按实体 id 拉数据，保持顶层路径（分享/收藏链接稳定）
          { path: 'voyages/:id', element: page(() => import('../features/voyages/VoyageDetailPage'), 'VoyageDetailPage') },
          { path: 'papers/:id/read', element: page(() => import('../features/reading/ReadingPage'), 'ReadingPage') },
          { path: 'ideas/:id', element: page(() => import('../features/forge/IdeaDetailPage'), 'IdeaDetailPage') },
          { path: 'experiment/:id', element: page(() => import('../features/experiment/ExperimentDetailPage'), 'ExperimentDetailPage') },
          { path: 'writer/:id', element: page(() => import('../features/writer/WriterEditorPage'), 'WriterEditorPage') },
          // 旧路径重定向：跳到当前课题下的同名子页面
          { index: true, element: <LegacyTopicRedirect /> },
          { path: 'wiki', element: <LegacyTopicRedirect sub="wiki" /> },
          { path: 'research', element: <LegacyTopicRedirect sub="research" /> },
          { path: 'forge', element: <LegacyTopicRedirect sub="forge" /> },
          { path: 'review', element: <LegacyTopicRedirect sub="review" /> },
          { path: 'experiment', element: <LegacyTopicRedirect sub="experiment" /> },
          { path: 'writer', element: <LegacyTopicRedirect sub="writer" /> },
          { path: 'paper-review', element: <LegacyTopicRedirect sub="paper-review" /> },
          { path: 'voyages', element: <LegacyTopicRedirect sub="voyages" /> },
        ],
      },
      // —— 非课题作用域 ——
      { path: 'start', element: page(() => import('../features/start/StartPage'), 'StartPage') },
      { path: 'projects/new', element: page(() => import('../features/projects/ProjectWizardPage'), 'ProjectWizardPage') },
      { path: 'projects/:id', element: page(() => import('../features/projects/ProjectDetailPage'), 'ProjectDetailPage') },
      { path: 'join/:token', element: page(() => import('../features/projects/JoinPage'), 'JoinPage') },
      { path: 'library', element: page(() => import('../features/library/LibraryPage'), 'LibraryPage') },
      // 实验室区：共享方向文献库（全实验室可读，无需课题）
      { path: 'libraries', element: page(() => import('../features/libraries/LibrariesPage'), 'LibrariesPage') },
      { path: 'libraries/:id', element: page(() => import('../features/libraries/LibraryDetailPage'), 'LibraryDetailPage') },
      // MCP 说明已并入设置页签，旧链接重定向
      { path: 'mcp-tools', element: <Navigate to="/settings?tab=mcp" replace /> },
      { path: 'skills', element: page(() => import('../features/skills/SkillsPage'), 'SkillsPage') },
      { path: 'settings', element: page(() => import('../features/settings/SettingsPage'), 'SettingsPage') },
    ],
  },
]);
