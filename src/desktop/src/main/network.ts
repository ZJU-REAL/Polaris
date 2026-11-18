/* ============================================================
   联网设置：首启安装要下载 Python 和依赖，网络不通时用户得有办法。

   代理按这个顺序取，取到就停：
   1. 设置里填的（设置 → 关于 → 网络，或安装失败页）；
   2. 环境变量 HTTPS_PROXY / ALL_PROXY 等——从终端启动、或登录 shell 里
      export 过的（kernel 启动时会向登录 shell 要一次）；
   3. 系统代理（macOS「网络」设置、Windows 的 Internet 选项）。从访达启动
      的应用拿不到 shell 的环境变量，但 Chromium 知道系统代理是什么。

   第 1、3 条的结果由本模块交给引导（engine-bootstrap 再写进 uv 的环境）；
   第 2 条什么都不用做，子进程自然继承。

   镜像策略同样存在这里：auto（默认，官方源不通再换国内镜像）/ on / off。
   调试时可用环境变量盖过设置：POLARIS_PROXY、POLARIS_BOOTSTRAP_MIRROR。
   ============================================================ */

import { session } from 'electron';

import type { NetworkSettings } from '../shared/contract';
import type { BootstrapNetwork, MirrorMode } from './engine-bootstrap';
import { readConfig, writeConfig } from './store';

const PROXY_SCHEMES = new Set(['http:', 'https:', 'socks5:', 'socks5h:', 'socks4:']);

/** 规范化用户填的代理地址；空串 = 清除。不合法抛错（消息给界面直接显示）。 */
export function normalizeProxy(raw: string): string {
  const text = raw.trim();
  if (!text) return '';
  // 只写了 host:port 的，按 http 代理理解（最常见的写法）
  const candidate = /^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `http://${text}`;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    throw new Error('ERR_INVALID_PARAMS: proxy must look like http://127.0.0.1:7890');
  }
  if (!PROXY_SCHEMES.has(url.protocol) || !url.hostname) {
    throw new Error('ERR_INVALID_PARAMS: proxy must be an http://, https:// or socks5:// address');
  }
  return candidate.replace(/\/+$/, '');
}

function mirrorFromEnv(): MirrorMode | null {
  const raw = process.env.POLARIS_BOOTSTRAP_MIRROR?.trim().toLowerCase();
  return raw === 'on' || raw === 'off' || raw === 'auto' ? raw : null;
}

export function getNetworkSettings(): NetworkSettings {
  const stored = readConfig().network;
  return { proxy: stored.proxy, mirror: stored.mirror };
}

export function setNetworkSettings(next: { proxy: string; mirror: string }): NetworkSettings {
  const mirror: MirrorMode = next.mirror === 'on' || next.mirror === 'off' ? next.mirror : 'auto';
  writeConfig({ network: { proxy: normalizeProxy(next.proxy), mirror } });
  return getNetworkSettings();
}

/** Chromium 的代理解析结果（"PROXY host:port; DIRECT" 这种）→ 一个代理地址，或 null。 */
export function parseProxyRule(rule: string): string | null {
  for (const part of rule.split(';')) {
    const [kind, target] = part.trim().split(/\s+/, 2);
    if (!kind || !target) continue;
    const type = kind.toUpperCase();
    if (type === 'PROXY' || type === 'HTTP') return `http://${target}`;
    if (type === 'HTTPS') return `https://${target}`;
    if (type === 'SOCKS' || type === 'SOCKS5') return `socks5://${target}`;
    if (type === 'SOCKS4') return `socks4://${target}`;
  }
  return null;
}

function envHasProxy(): boolean {
  return ['HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy', 'HTTP_PROXY', 'http_proxy'].some(
    (name) => !!process.env[name],
  );
}

/** 用户明确指定的代理（设置或 POLARIS_PROXY）；没有则为空串。 */
export function configuredProxy(): string {
  const override = process.env.POLARIS_PROXY?.trim();
  if (override) {
    try {
      return normalizeProxy(override);
    } catch {
      console.warn('[network] POLARIS_PROXY 不是合法的代理地址，忽略');
    }
  }
  return getNetworkSettings().proxy;
}

/**
 * 引导要用的联网参数。proxy 为 null 表示「不另外指定」：环境变量里有就继承，
 * 没有就直连。来源只记种类，地址本身可能带账号密码，不进日志。
 */
export async function resolveBootstrapNetwork(): Promise<BootstrapNetwork & { proxySource: string }> {
  const mirror = mirrorFromEnv() ?? getNetworkSettings().mirror;
  const explicit = configuredProxy();
  if (explicit) return { proxy: explicit, mirror, proxySource: 'setting' };
  if (envHasProxy()) return { proxy: null, mirror, proxySource: 'environment' };
  try {
    const rule = await session.defaultSession.resolveProxy('https://pypi.org/');
    const system = parseProxyRule(rule);
    if (system) return { proxy: system, mirror, proxySource: 'system' };
  } catch {
    /* 查不到系统代理就当没有 */
  }
  return { proxy: null, mirror, proxySource: 'none' };
}
