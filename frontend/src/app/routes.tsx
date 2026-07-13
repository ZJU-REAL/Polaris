import { createBrowserRouter } from 'react-router-dom';
import { AppShell } from './AppShell';
import { RequireAuth } from './auth';
import { LoginPage } from '../features/auth/LoginPage';
import { DashboardPage } from '../features/dashboard/DashboardPage';
import { WikiPage } from '../features/wiki/WikiPage';
import { ForgePage } from '../features/forge/ForgePage';
import { ReviewPage } from '../features/review/ReviewPage';
import { ExperimentPage } from '../features/experiment/ExperimentPage';
import { WriterPage } from '../features/writer/WriterPage';
import { PaperReviewPage } from '../features/paper-review/PaperReviewPage';
import { SettingsPage } from '../features/settings/SettingsPage';

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: (
      <RequireAuth>
        <AppShell />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'wiki', element: <WikiPage /> },
      { path: 'forge', element: <ForgePage /> },
      { path: 'review', element: <ReviewPage /> },
      { path: 'experiment', element: <ExperimentPage /> },
      { path: 'writer', element: <WriterPage /> },
      { path: 'paper-review', element: <PaperReviewPage /> },
      { path: 'settings', element: <SettingsPage /> },
    ],
  },
]);
