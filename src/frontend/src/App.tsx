import { Fragment } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';
import { AuthProvider } from './app/auth';
import { router } from './app/routes';
import { useLang } from './lib/i18n';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

export function App() {
  // 语言切换时整树重挂载：所有 tr() 文案重新求值（Query 缓存在 Provider 外层，保留）
  const lang = useLang();
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Fragment key={lang}>
          <RouterProvider router={router} />
        </Fragment>
      </AuthProvider>
    </QueryClientProvider>
  );
}
