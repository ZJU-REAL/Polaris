import { Fragment, useEffect, useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';
import { AuthProvider } from './app/auth';
import { router } from './app/routes';
import { ServerSetupPage } from './features/desktop/ServerSetupPage';
import { isDesktop, serverOrigin } from './lib/endpoint';
import { onHostEvent } from './lib/host';
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

  // 桌面端：未配置服务器时先进配置页；已配置时可从原生菜单「Server…」再次进入。
  // 这一分支在 AuthProvider 之外，确保没有任何 API 请求先于服务器地址确定发出。
  const [setupOpen, setSetupOpen] = useState(() => isDesktop() && !serverOrigin());
  useEffect(
    () =>
      onHostEvent((e) => {
        if (e.type === 'host.openServerSetup') setSetupOpen(true);
      }),
    [],
  );

  if (setupOpen) {
    return (
      <Fragment key={lang}>
        <ServerSetupPage onCancel={serverOrigin() ? () => setSetupOpen(false) : undefined} />
      </Fragment>
    );
  }

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
