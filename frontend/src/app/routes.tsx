import { createBrowserRouter } from 'react-router-dom';
import { AppShell } from './AppShell';
import { RequireAuth } from './auth';
import { ProjectProvider } from './project';
import { LoginPage } from '../features/auth/LoginPage';
import { DashboardPage } from '../features/dashboard/DashboardPage';
import { ProjectWizardPage } from '../features/projects/ProjectWizardPage';
import { ProjectDetailPage } from '../features/projects/ProjectDetailPage';
import { VoyagesPage } from '../features/voyages/VoyagesPage';
import { VoyageDetailPage } from '../features/voyages/VoyageDetailPage';
import { WikiPage } from '../features/wiki/WikiPage';
import { ForgePage } from '../features/forge/ForgePage';
import { IdeaDetailPage } from '../features/forge/IdeaDetailPage';
import { ReviewPage } from '../features/review/ReviewPage';
import { ExperimentPage } from '../features/experiment/ExperimentPage';
import { ExperimentDetailPage } from '../features/experiment/ExperimentDetailPage';
import { WriterPage } from '../features/writer/WriterPage';
import { PaperReviewPage } from '../features/paper-review/PaperReviewPage';
import { SettingsPage } from '../features/settings/SettingsPage';

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
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
      { index: true, element: <DashboardPage /> },
      { path: 'projects/new', element: <ProjectWizardPage /> },
      { path: 'projects/:id', element: <ProjectDetailPage /> },
      { path: 'voyages', element: <VoyagesPage /> },
      { path: 'voyages/:id', element: <VoyageDetailPage /> },
      { path: 'wiki', element: <WikiPage /> },
      { path: 'forge', element: <ForgePage /> },
      { path: 'ideas/:id', element: <IdeaDetailPage /> },
      { path: 'review', element: <ReviewPage /> },
      { path: 'experiment', element: <ExperimentPage /> },
      { path: 'experiment/:id', element: <ExperimentDetailPage /> },
      { path: 'writer', element: <WriterPage /> },
      { path: 'paper-review', element: <PaperReviewPage /> },
      { path: 'settings', element: <SettingsPage /> },
    ],
  },
]);
