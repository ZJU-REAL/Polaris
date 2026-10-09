import { app, clipboard, shell } from 'electron';

import type { HostInfo } from '../../shared/contract';
import { effectiveVersion } from '../updates/renderer-store';

export function hostInfo(): HostInfo {
  return {
    platform: process.platform as HostInfo['platform'],
    appVersion: effectiveVersion(),
  };
}

/**
 * 退出并重新打开：走正常退出路径（before-quit → stopKernel 回收引擎子进程），
 * 下次启动重新引导、重新拉起引擎。
 */
export function relaunch(): void {
  app.relaunch();
  app.quit();
}

export function openExternal(url: string): void {
  try {
    const u = new URL(url);
    if (u.protocol === 'http:' || u.protocol === 'https:') void shell.openExternal(url);
  } catch {
    /* 非法 URL 静默忽略 */
  }
}

export async function copyText(text: string): Promise<boolean> {
  // Electron 44 起 clipboard 对齐 W3C 异步 API，writeText 返回 Promise。
  await clipboard.writeText(text);
  return true;
}

export function setBadgeCount(count: number): void {
  // Windows 需要 overlay icon 才能显示角标，一期不做——app.setBadgeCount 在
  // Windows 上返回 false，这里不当作错误。
  if (process.platform === 'win32') return;
  app.setBadgeCount(Math.max(0, Math.floor(count)));
}
