/* 原生应用菜单。

   这不是「增值功能」而是阻塞项：macOS 上如果不注册带 role:'editMenu' 的菜单，
   Cmd+C / Cmd+V / Cmd+A / Cmd+Z 会在部分控件里直接失效 —— CodeMirror 编辑器
   和输入框会变得不可用。 */

import { Menu, app, shell, type MenuItemConstructorOptions } from 'electron';

import { IPC_CHANNEL_EVENT } from '../shared/contract';
import { getWindow } from './window';

const REPO_URL = 'https://github.com/ZJU-REAL/Polaris';

function openServerSetup(): void {
  getWindow()?.webContents.send(IPC_CHANNEL_EVENT, { type: 'host.openServerSetup' });
}

export function installMenu(): void {
  const isMac = process.platform === 'darwin';

  const serverItem: MenuItemConstructorOptions = {
    label: 'Server…',
    accelerator: 'CmdOrCtrl+,',
    click: openServerSetup,
  };

  const template: MenuItemConstructorOptions[] = [
    ...(isMac
      ? ([
          {
            label: app.name,
            submenu: [
              { role: 'about' },
              { type: 'separator' },
              serverItem,
              { type: 'separator' },
              { role: 'services' },
              { type: 'separator' },
              { role: 'hide' },
              { role: 'hideOthers' },
              { role: 'unhide' },
              { type: 'separator' },
              { role: 'quit' },
            ],
          },
        ] as MenuItemConstructorOptions[])
      : []),
    {
      label: 'File',
      submenu: isMac
        ? [{ role: 'close' }]
        : [serverItem, { type: 'separator' }, { role: 'quit' }],
    },
    // ★ 这一项是 macOS 剪贴板快捷键能工作的原因
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
    {
      role: 'help',
      submenu: [{ label: 'Project on GitHub', click: () => void shell.openExternal(REPO_URL) }],
    },
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}
