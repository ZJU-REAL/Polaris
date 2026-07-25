import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { hostPlatform } from './lib/host';
import './styles/global.css';

// 桌面端把平台标在 <html> 上：macOS 的 hiddenInset 标题栏需要页面自己给
// 交通灯留出顶部空间（见 global.css 的 --titlebar-h）。web 端不设此属性。
const platform = hostPlatform();
if (platform) {
  document.documentElement.dataset.desktopPlatform = platform;
}

const container = document.getElementById('root');
if (!container) {
  throw new Error('#root element not found');
}

const root = createRoot(container);

// 桌面端先把钥匙串里的会话读出来再渲染：AuthProvider 在初始化时同步取 token，
// 晚一步就会先闪一下登录页。web 端 installDesktopTokenStore 直接返回。
void import('./lib/session')
  .then(({ installDesktopTokenStore }) => installDesktopTokenStore())
  .catch(() => {
    /* 钥匙串不可用：保持 localStorage，不阻断启动 */
  })
  .finally(() => {
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  });
