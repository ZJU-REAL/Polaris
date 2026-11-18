import { app, shell } from 'electron';

import { emit } from './ipc/events';
import { installIpc } from './ipc/router';
import { onEngineState, startKernel, stopKernel } from './kernel';
import { installMainLog } from './logging';
import { installMenu } from './menu';
import { APP_ORIGIN, handleAppProtocol, registerAppScheme } from './protocol';
import { createWindow, getWindow } from './window';

/** 退出时给内核停机留的时间。到点还没停完就直接退出，不让「退出」变成「没反应」。 */
const QUIT_DEADLINE_MS = 10_000;

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
  // 主进程日志落盘（<userData>/logs/main.log）。放在单实例锁之后：被踢掉的
  // 第二个实例不该往正在运行的那个实例的日志里写东西。
  installMainLog();

  app.on('second-instance', () => {
    const win = getWindow();
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  void app.whenReady().then(() => {
    handleAppProtocol();
    installIpc();
    installMenu();
    // 引擎中途退出 / 自动重启 / 没救回来：转给界面，由它换到等待页或兜底页
    onEngineState((event) => emit({ type: 'engine.state', ...event }));
    // 窗口先起、内核后台启动（#721）：打包态首启的内嵌引擎引导要下载
    // Python 工具链、装依赖，可能长达数分钟——以前 await startKernel 在
    // createWindow 之前，用户这几分钟连窗口都看不到，像没装上一样。
    // 现在渲染层经 kernel.engineBootstrapStatus 轮询进度并显示首启等待页；
    // 期间 kernel.status/localBackend 读到「还没启动」是真实状态而不是假象，
    // 等待页正是为消化这个窗口期而存在。
    createWindow();
    void startKernel().catch((err) => console.error('[kernel] start failed:', err));

    app.on('activate', () => {
      if (!getWindow()) createWindow();
    });
  });

  // 退出路径统一走 before-quit（cmd+Q、菜单退出、window-all-closed 的 app.quit
  // 都会经过这里）。kernel.stop 是异步的（fiber.dispose 级联回收），而 Electron
  // 的退出流程不等 Promise，所以第一次先拦下退出、停完内核再重新 quit。
  // stopKernel 会打断还在进行的启动（首启下载、等引擎就绪），不等它们跑完；
  // 再加一个时限兜底——无论停机卡在哪，用户按了退出，应用就一定会退出。
  let kernelStopping = false;
  app.on('before-quit', (event) => {
    if (kernelStopping) return;
    kernelStopping = true;
    event.preventDefault();
    // 窗口先收起来：停机可能要几秒（等引擎进程退出），别让用户对着一个
    // 不再响应的窗口以为退出没生效
    getWindow()?.hide();
    const deadline = setTimeout(() => {
      console.error(`[kernel] stop did not finish within ${QUIT_DEADLINE_MS}ms, exiting anyway`);
      app.exit(0);
    }, QUIT_DEADLINE_MS);
    void stopKernel()
      .catch((err) => console.error('[kernel] stop failed:', err))
      .finally(() => {
        clearTimeout(deadline);
        app.quit();
      });
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
