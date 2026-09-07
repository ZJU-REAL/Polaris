import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { EngineBootstrapPage } from './features/desktop/EngineBootstrapPage';
import { bootstrapGate } from './features/desktop/engineBootstrap';
import { probeLocalBackend } from './lib/endpoint';
import { engineBootstrapStatus, hasHost, hostPlatform } from './lib/host';
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

const root = createRoot(container);
const renderApp = () =>
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );

// 桌面端挂载前的两步探测（各一次 IPC 往返，毫秒级）；web 端没有宿主桥，
// 保持原来的同步挂载路径，行为一字不变。
// 1. 内核引导状态（#721）：窗口现在先于内核创建，打包态首启内核还在后台
//    装环境——此刻探测本地引擎只会得到 null 然后误落远端流程。未就绪就
//    先进等待页，就绪/失败后由等待页负责重探或给出远程出路。
// 2. 本地引擎地址：让首屏请求就走对地址，而不是发出去之后才发现该走
//    127.0.0.1。
if (hasHost()) {
  void engineBootstrapStatus().then((status) => {
    if (status == null || bootstrapGate(status) === 'proceed') {
      void probeLocalBackend().finally(renderApp);
      return;
    }
    root.render(
      <StrictMode>
        <EngineBootstrapPage
          initialStatus={status}
          onProceed={() => void probeLocalBackend().finally(renderApp)}
        />
      </StrictMode>,
    );
  });
} else {
  renderApp();
}
