import { useEffect } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';
import { AuthProvider } from './app/auth';
import { router } from './app/routes';
import { EngineUnavailablePage } from './features/desktop/EngineUnavailablePage';
import { isDesktop, localEngineProblem, localOrigin } from './lib/endpoint';
import { loadCapabilities } from './lib/host';

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
   状态归零，填了一半的表单切一下语言就全没了（见 issue #377）。

   现在按「谁需要重挂载谁负责」拆开：
   - 桌面端的独立页面自己订阅 useLang()，就地重渲染，tr() 照样重新求值，
     但页面状态留着；
   - 壳层内的业务页仍由 AppShell 以 key={lang} 重挂载（那些页面的文案散在深层子树里，
     且元素来自路由表的稳定引用，父组件重渲染带不动它们）。 */
export function App() {
  // 能力清单拉一次即可。拉到之前 isCapabilityAvailable() 一律返回 false。
  useEffect(() => {
    void loadCapabilities();
  }, []);

  // 桌面端：main.tsx 挂载前已探测过本机引擎地址。拿不到就没有可用的后端，
  // 在 AuthProvider 之外拦下，确保不会有请求打向不存在的地址。
  if (isDesktop() && localOrigin() == null) {
    return <EngineUnavailablePage problem={localEngineProblem()} />;
  }

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>
  );
}
