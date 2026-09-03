import { app, shell } from 'electron';

import { installIpc } from './ipc/router';
import { startKernel, stopKernel } from './kernel';
import { installMenu } from './menu';
import { APP_ORIGIN, handleAppProtocol, registerAppScheme } from './protocol';
import { readConfig } from './store';
import { createWindow, getWindow } from './window';

/**
 * 深链接 scheme。一期 handler 是空的，但**注册必须现在做**：协议注册写在打包
 * 配置与系统注册表里，等第二期再加就要求所有人重装应用。
 */
const DEEP_LINK_SCHEME = 'polaris';

// 测试挂钩：E2E 用临时 userData 起壳，既保证首启状态干净（config.json /
// localStorage 都是空的），也让单实例锁按目录隔离——不会与用户正开着的
// 正式实例互踢。必须在 requestSingleInstanceLock 与一切读 userData 的
// 代码之前设好。正常启动不设此 env，行为一字不变。
const userDataOverride = process.env.POLARIS_USER_DATA_DIR;
if (userDataOverride) app.setPath('userData', userDataOverride);

// 必须早于 app.whenReady()
registerAppScheme();

if (!app.requestSingleInstanceLock()) {
  // 多开会让两个实例互相覆盖 config.json，直接退出第二个
  app.quit();
} else {
  app.on('second-instance', () => {
    const win = getWindow();
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  app.on('open-url', (event) => {
    event.preventDefault();
    // 一期不处理跳转目标，只保证协议已注册、不会把 URL 当文件打开
  });

  void app.whenReady().then(async () => {
    // 内核先于 IPC：installIpc 一装好 renderer 就可能打 kernel.status，
    // 不能让它读到「还没启动」的假象。start 本身不做 IO，不拖慢首窗。
    await startKernel();
    app.setAsDefaultProtocolClient(DEEP_LINK_SCHEME);
    handleAppProtocol(() => readConfig().serverUrl);
    installIpc();
    installMenu();
    createWindow();

    app.on('activate', () => {
      if (!getWindow()) createWindow();
    });
  });

  // 退出路径统一走 before-quit（cmd+Q、菜单退出、window-all-closed 的 app.quit
  // 都会经过这里）。kernel.stop 是异步的（fiber.dispose 级联回收），而 Electron
  // 的退出流程不等 Promise，所以第一次先拦下退出、停完内核再重新 quit。
  let kernelStopping = false;
  app.on('before-quit', (event) => {
    if (kernelStopping) return;
    kernelStopping = true;
    event.preventDefault();
    void stopKernel()
      .catch((err) => console.error('[kernel] stop failed:', err))
      .finally(() => app.quit());
  });

  // 全局兜底：任何新建的 webContents（含未来可能出现的子窗口）都不允许
  // 在应用内导航到外部地址。window.ts 里已针对主窗口设过一次，这里防漏。
  app.on('web-contents-created', (_event, contents) => {
    contents.setWindowOpenHandler(({ url }) => {
      if (url.startsWith('http://') || url.startsWith('https://')) void shell.openExternal(url);
      return { action: 'deny' };
    });
    contents.on('will-navigate', (event, url) => {
      if (!url.startsWith(APP_ORIGIN)) event.preventDefault();
    });
  });

  app.on('window-all-closed', () => {
    // macOS 保持标准行为（关窗不退出）；其余平台关窗即退出。
    // 托盘常驻是另一套语义，一期不做。
    if (process.platform !== 'darwin') app.quit();
  });
}
