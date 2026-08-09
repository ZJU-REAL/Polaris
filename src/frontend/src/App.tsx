import { useEffect, useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';
import { AuthProvider } from './app/auth';
import { router } from './app/routes';
import { ServerSetupPage } from './features/desktop/ServerSetupPage';
import { isDesktop, serverOrigin } from './lib/endpoint';
import { loadCapabilities, onHostEvent } from './lib/host';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

/* 语言切换**不在这里**重挂载整棵树。

   以前这里是 <Fragment key={lang}>：换一次语言，从路由往下的一切全部重建，所有页面
   状态归零。代价最扎眼的地方是登录页——填了一半的注册信息、选中的「注册」标签，切一下
   语言就全没了，账号输入框还退回上一次登录的账号（见 issue #377）。

   现在按「谁需要重挂载谁负责」拆开：
   - 登录页与桌面端服务器配置页自己订阅 useLang()，就地重渲染，tr() 照样重新求值，
     但输入框里的东西留着；
   - 壳层内的业务页仍由 AppShell 以 key={lang} 重挂载（那些页面的文案散在深层子树里，
     且元素来自路由表的稳定引用，父组件重渲染带不动它们）。 */
export function App() {
  // 桌面端：未配置服务器时先进配置页；已配置时可从原生菜单「Server…」再次进入。
  // 这一分支在 AuthProvider 之外，确保没有任何 API 请求先于服务器地址确定发出。
  const [setupOpen, setSetupOpen] = useState(() => isDesktop() && !serverOrigin());
  // 能力清单拉一次即可（换服务器会重建窗口）。拉到之前 isCapabilityAvailable()
  // 一律返回 false，即默认走服务器——这是安全的方向。
  useEffect(() => {
    void loadCapabilities();
  }, []);

  useEffect(
    () =>
      onHostEvent((e) => {
        if (e.type === 'host.openServerSetup') setSetupOpen(true);
      }),
    [],
  );

  if (setupOpen) {
    return <ServerSetupPage onCancel={serverOrigin() ? () => setSetupOpen(false) : undefined} />;
  }

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>
  );
}
