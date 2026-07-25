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

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
