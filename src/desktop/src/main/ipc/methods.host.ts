import { app, clipboard, net, shell } from 'electron';

import type { HostInfo, ServerProbe } from '../../shared/contract';
import { readConfig, writeConfig } from '../store';
import { recreateWindow } from '../window';

const PROBE_TIMEOUT_MS = 5_000;

export function hostInfo(): HostInfo {
  return {
    serverUrl: readConfig().serverUrl,
    platform: process.platform as HostInfo['platform'],
    appVersion: app.getVersion(),
  };
}

/** 归一化并校验服务器地址：只接受 http/https，去掉尾斜杠。非法返回 null。 */
export function normalizeServerUrl(raw: string): string | null {
  const trimmed = (raw ?? '').trim();
  if (!trimmed) return null;
  try {
    const u = new URL(trimmed);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return `${u.origin}${u.pathname.replace(/\/+$/, '')}`;
  } catch {
    return null;
  }
}

/**
 * 探测服务器：打无鉴权的 GET /api/health 并校验响应体形状。
 * 只判断 HTTP 200 是不够的——那样连到任意一台 web 服务器都会显示「成功」。
 */
export async function testServer(url: string): Promise<ServerProbe> {
  const base = normalizeServerUrl(url);
  if (!base) return { ok: false, reason: 'invalid-url' };
  try {
    const res = await net.fetch(`${base}/api/health`, {
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!res.ok) return { ok: false, reason: 'not-polaris', detail: `HTTP ${res.status}` };
    const body = (await res.json()) as { status?: unknown; version?: unknown };
    if (body?.status !== 'ok' || typeof body.version !== 'string') {
      return { ok: false, reason: 'not-polaris' };
    }
    return { ok: true, version: body.version };
  } catch (err) {
    const name = err instanceof Error ? err.name : '';
    if (name === 'TimeoutError' || name === 'AbortError') return { ok: false, reason: 'timeout' };
    return {
      ok: false,
      reason: 'unreachable',
      detail: err instanceof Error ? err.message : String(err),
    };
  }
}

export function setServerUrl(url: string): void {
  const base = normalizeServerUrl(url);
  if (!base) throw new Error('invalid server url');
  writeConfig({ serverUrl: base });
  // 重建而不是 reload：preload 注入值与 CSP 头都在文档加载时定下，reload 刷不掉。
  recreateWindow();
}

export function openExternal(url: string): void {
  try {
    const u = new URL(url);
    if (u.protocol === 'http:' || u.protocol === 'https:') void shell.openExternal(url);
  } catch {
    /* 非法 URL 静默忽略 */
  }
}

export function copyText(text: string): boolean {
  clipboard.writeText(text);
  return true;
}

export function setBadgeCount(count: number): void {
  // Windows 需要 overlay icon 才能显示角标，一期不做——app.setBadgeCount 在
  // Windows 上返回 false，这里不当作错误。
  if (process.platform === 'win32') return;
  app.setBadgeCount(Math.max(0, Math.floor(count)));
}
