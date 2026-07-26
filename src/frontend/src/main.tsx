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
  // 这两个平台都不画系统标题栏，窗口控件是覆盖在内容之上的，页面必须自己留出
  // 顶部空间。共用的留白规则挂这个标记；控件在左还是在右的差异才按平台区分。
  if (platform === 'darwin' || platform === 'win32') {
    document.documentElement.dataset.desktopTitlebar = 'overlay';
  }
}

const container = document.getElementById('root');
if (!container) {
  throw new Error('#root element not found');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
