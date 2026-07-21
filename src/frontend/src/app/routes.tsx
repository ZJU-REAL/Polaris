import { Suspense, lazy, type ComponentType, type ReactNode } from 'react';
import { createBrowserRouter } from 'react-router-dom';
import { AppShell } from './AppShell';
import { RequireAuth } from './auth';
import { ProjectProvider } from './project';

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
      { index: true, element: page(() => import('../features/dashboard/DashboardPage'), 'DashboardPage') },
      { path: 'projects/new', element: page(() => import('../features/projects/ProjectWizardPage'), 'ProjectWizardPage') },
      { path: 'projects/:id', element: page(() => import('../features/projects/ProjectDetailPage'), 'ProjectDetailPage') },
      { path: 'join/:token', element: page(() => import('../features/projects/JoinPage'), 'JoinPage') },
      { path: 'voyages', element: page(() => import('../features/voyages/VoyagesPage'), 'VoyagesPage') },
      { path: 'voyages/:id', element: page(() => import('../features/voyages/VoyageDetailPage'), 'VoyageDetailPage') },
      { path: 'library', element: page(() => import('../features/library/LibraryPage'), 'LibraryPage') },
      { path: 'wiki', element: page(() => import('../features/wiki/WikiPage'), 'WikiPage') },
      { path: 'papers/:id/read', element: page(() => import('../features/reading/ReadingPage'), 'ReadingPage') },
      { path: 'forge', element: page(() => import('../features/forge/ForgePage'), 'ForgePage') },
      { path: 'ideas/:id', element: page(() => import('../features/forge/IdeaDetailPage'), 'IdeaDetailPage') },
      { path: 'review', element: page(() => import('../features/review/ReviewPage'), 'ReviewPage') },
      { path: 'experiment', element: page(() => import('../features/experiment/ExperimentPage'), 'ExperimentPage') },
      { path: 'experiment/:id', element: page(() => import('../features/experiment/ExperimentDetailPage'), 'ExperimentDetailPage') },
      { path: 'writer', element: page(() => import('../features/writer/WriterPage'), 'WriterPage') },
      { path: 'writer/:id', element: page(() => import('../features/writer/WriterEditorPage'), 'WriterEditorPage') },
      { path: 'paper-review', element: page(() => import('../features/paper-review/PaperReviewPage'), 'PaperReviewPage') },
      { path: 'mcp-tools', element: page(() => import('../features/mcp/McpToolsPage'), 'McpToolsPage') },
      { path: 'skills', element: page(() => import('../features/skills/SkillsPage'), 'SkillsPage') },
      { path: 'settings', element: page(() => import('../features/settings/SettingsPage'), 'SettingsPage') },
    ],
  },
]);
