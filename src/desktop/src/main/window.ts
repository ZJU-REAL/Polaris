import { BrowserWindow, screen, shell } from 'electron';
import { join } from 'node:path';

import { APP_INDEX, APP_ORIGIN } from './protocol';
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

export function createWindow(): BrowserWindow {
  const config = readConfig();
  const win = new BrowserWindow({
    ...restoredBounds(config.window),
    minWidth: 960,
    minHeight: 600,
    show: false,
    backgroundColor: '#ffffff',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
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
