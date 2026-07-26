import { BrowserWindow, screen, shell } from 'electron';
import { join } from 'node:path';

import { IPC_CHANNEL_EVENT } from '../shared/contract';
import { APP_INDEX, APP_ORIGIN } from './protocol';
import { clearStagedRenderer, stagedRendererDir } from './updates/renderer-store';
import { readConfig, writeConfig, type WindowState } from './store';

let current: BrowserWindow | null = null;

export function getWindow(): BrowserWindow | null {
  return current;
}

function isSafeExternal(raw: string): boolean {
  try {
    const u = new URL(raw);
    // 协议白名单：只放 http/https。放开 file:/smb: 等于给外链一个本地资源读取面。
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch {
    return false;
  }
}

/** 恢复上次的窗口位置；保存的坐标已不在任何显示器上时回落居中。 */
function restoredBounds(state: WindowState) {
  const base = { width: state.width, height: state.height };
  if (state.x === undefined || state.y === undefined) return base;
  const visible = screen.getAllDisplays().some((d) => {
    const a = d.workArea;
    return (
      state.x! < a.x + a.width &&
      state.x! + state.width > a.x &&
      state.y! < a.y + a.height &&
      state.y! + state.height > a.y
    );
  });
  return visible ? { ...base, x: state.x, y: state.y } : base;
}

function persistBounds(win: BrowserWindow): void {
  if (win.isDestroyed()) return;
  const maximized = win.isMaximized();
  // 最大化状态下取 normal bounds，否则「取消最大化」会还原成全屏尺寸
  const b = maximized ? win.getNormalBounds() : win.getBounds();
  writeConfig({ window: { x: b.x, y: b.y, width: b.width, height: b.height, maximized } });
}

const isMac = process.platform === 'darwin';
const isWin = process.platform === 'win32';

/**
 * 非 macOS 去掉了菜单栏（见 menu.ts），随之丢掉的是菜单绑定的快捷键。
 * 剪贴板类（Ctrl+C/V/X/A/Z）由 Chromium 在渲染层直接处理，不受影响；
 * 这里补回挂在菜单上的那些。
 *
 * Ctrl+, 尤其不能漏：它是「换服务器」的**唯一**入口（菜单里的 Server…）。
 * 少了它，用户在 Windows 上只能靠删 %APPDATA%\polaris-desktop 才能改地址。
 */
function installKeyboardShortcuts(win: BrowserWindow): void {
  if (isMac) return;
  win.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const ctrl = input.control || input.meta;
    const key = input.key.toLowerCase();
    const fire = (fn: () => void) => {
      fn();
      event.preventDefault();
    };
    if (key === 'f5' || (ctrl && key === 'r')) return fire(() => win.webContents.reload());
    if (key === 'f12' || (ctrl && input.shift && key === 'i')) {
      return fire(() => win.webContents.toggleDevTools());
    }
    if (!ctrl) return;
    if (key === ',') {
      return fire(() => win.webContents.send(IPC_CHANNEL_EVENT, { type: 'host.openServerSetup' }));
    }
    const zoom = win.webContents.getZoomLevel();
    if (key === '=' || key === '+') return fire(() => win.webContents.setZoomLevel(zoom + 0.5));
    if (key === '-') return fire(() => win.webContents.setZoomLevel(zoom - 0.5));
    if (key === '0') return fire(() => win.webContents.setZoomLevel(0));
  });
}

export function createWindow(): BrowserWindow {
  const config = readConfig();
  const win = new BrowserWindow({
    ...restoredBounds(config.window),
    minWidth: 960,
    minHeight: 600,
    show: false,
    backgroundColor: '#ffffff',
    // 沉浸式标题栏：macOS 与 Windows 都不要系统标题栏，窗口控件以覆盖层形式
    // 画在内容之上。Linux 的窗口管理器差异太大（有的根本不画覆盖层），保持原生
    // 标题栏，稳妥。
    titleBarStyle: isMac ? 'hiddenInset' : isWin ? 'hidden' : 'default',
    // 把交通灯钉在这条 40px 留白带的垂直中心（(40-12)/2 = 14），不同 macOS 版本
    // 的默认内缩量不一样，钉死才能和 global.css 的 --titlebar-h 对得上。
    ...(isMac ? { trafficLightPosition: { x: 18, y: 14 } } : {}),
    // Windows 的最小化/最大化/关闭画在右上角，与顶栏同一行（顶栏右端由 CSS 的
    // --winctl-w 留出等宽空位）。配色取自 tokens.css，否则按钮区会是一块白底。
    ...(isWin
      ? {
          titleBarOverlay: {
            color: '#f7f9fc',
            symbolColor: '#59637a',
            // 与顶栏等高，按钮才会和铃铛排在同一行：顶栏是 52px CSS，
            // 经 html{zoom:1.1} 渲染成约 57 个窗口点。
            height: 57,
          },
        }
      : {}),
    webPreferences: {
      preload: join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      nodeIntegrationInWorker: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      spellcheck: false,
      // ★ 必开：写作页的论文预览是 <iframe src=blob:…pdf>，走 Chromium 内置
      // pdfium（不是 pdf.js）。Electron 默认关插件，漏了这一项预览会全白。
      plugins: true,
    },
  });
  current = win;

  if (config.window.maximized) win.maximize();
  win.once('ready-to-show', () => win.show());

  // 外链一律不在 Electron 里开：全站 20+ 处 target="_blank"（arXiv、GitHub issue、
  // 论文源站），在应用内打开会得到一个没有地址栏、退不回去的窗口。
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isSafeExternal(url)) void shell.openExternal(url);
    return { action: 'deny' };
  });

  win.webContents.on('will-navigate', (event, url) => {
    if (url.startsWith(APP_ORIGIN)) return; // SPA 内部导航
    event.preventDefault();
    if (isSafeExternal(url)) void shell.openExternal(url);
  });

  // 当前不需要摄像头/麦克风/定位等任何权限，默认全拒。
  win.webContents.session.setPermissionRequestHandler((_wc, _permission, callback) => {
    callback(false);
  });

  installKeyboardShortcuts(win);

  // 热更新装上的界面若加载不起来，退回包内自带的那份再重来一次。
  // 只挡主框架的失败；子资源 404 由协议处理器各自处理，不该整窗回滚。
  win.webContents.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || !stagedRendererDir()) return;
    console.error(`[polaris] 界面加载失败（${code} ${desc} ${url}），回滚到内置版本`);
    clearStagedRenderer();
    void win.loadURL(APP_INDEX);
  });

  win.on('close', () => persistBounds(win));
  win.on('closed', () => {
    if (current === win) current = null;
  });

  void win.loadURL(APP_INDEX);
  return win;
}

/**
 * 换服务器后必须重建窗口而不是 reload：preload 注入值（window.__POLARIS__）
 * 与 index.html 的 CSP 响应头都是在文档加载时定下的，reload 刷不掉。
 */
export function recreateWindow(): void {
  const old = current;
  current = null;
  if (old && !old.isDestroyed()) {
    persistBounds(old);
    old.destroy();
  }
  createWindow();
}
