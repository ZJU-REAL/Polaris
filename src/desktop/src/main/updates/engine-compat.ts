/* ============================================================
   「只换界面」的更新到底能不能用——判据在这里，纯函数，不 import electron。

   安装包里有三样东西：外壳（Electron 主进程 + preload）、界面、Python 引擎。
   热更新只换界面。以前的判据只有契约版本号（界面与 preload 之间的约定），
   它管不了第三样：新界面可能调用了新引擎才有的接口。结果是老安装装上新界面、
   引擎还是老的，「关于」里显示的是新版本号，更新检查也认为自己已是最新——
   引擎的修复永远到不了这台机器。

   现在界面包自己声明它是配哪份引擎打出来的：
     renderer-<版本>-c<契约>-b<后端哈希前 16 位>.tar.gz
   后端哈希是打包时对引擎源码算的内容哈希（stage-backend.mjs 写进
   resources/backend/.hash，也是首启引导判断「要不要重装环境」用的同一个值）。
   只有它与本机已装的那份完全相同，才允许只换界面；否则走安装器。

   文件名换了格式还有一层作用：老外壳（只认 renderer-<版本>-c<契约>.tar.gz）
   认不出新包，自然回落到安装器——已经被旧逻辑带偏的安装也能借此回到正轨。
   ============================================================ */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/** 文件名里带的哈希前缀长度。16 个十六进制字符 = 64 位，足够区分版本。 */
export const BACKEND_HASH_PREFIX = 16;

const RENDERER_RE = /^renderer-(.+)-c(\d+)-b([0-9a-f]{12,64})\.tar\.gz$/;

export interface RendererAssetInfo {
  version: string;
  contract: number;
  /** 文件名里声明的后端哈希（前缀）。 */
  backendHash: string;
}

/** 解析界面包文件名；不是界面包（含旧格式）返回 null。 */
export function parseRendererAsset(name: string): RendererAssetInfo | null {
  const m = RENDERER_RE.exec(name);
  if (!m) return null;
  return { version: m[1]!, contract: Number(m[2]), backendHash: m[3]! };
}

/** 声明的哈希（可能是前缀）与本机已装的是否是同一份引擎。任一为空都算不是。 */
export function sameBackend(declared: string | null | undefined, installed: string | null | undefined): boolean {
  if (!declared || !installed) return false;
  if (declared.length < 12) return false;
  return installed.startsWith(declared) || declared.startsWith(installed);
}

/**
 * 本机安装包里那份引擎的内容哈希。从源码跑（开发态）没有这个文件，返回 null——
 * 此时一律不做只换界面的更新。
 */
export function installedBackendHash(resourcesDir: string | undefined = process.resourcesPath): string | null {
  if (!resourcesDir) return null;
  try {
    const hash = readFileSync(join(resourcesDir, 'backend', '.hash'), 'utf8').trim();
    return /^[0-9a-f]{12,64}$/.test(hash) ? hash : null;
  } catch {
    return null;
  }
}

export interface UpdateChoice<A> {
  kind: 'hot' | 'full';
  asset: A;
  /** kind=hot 时界面包的信息。 */
  renderer?: RendererAssetInfo;
  /** kind=full 时为什么不能只换界面。 */
  fullReason?: 'engine' | 'shell' | 'no-renderer';
}

/**
 * 从 release 的产物里挑这台机器该用的那个。
 * - 有界面包、契约不高于外壳、且声明的后端与已装的相同 → 只换界面；
 * - 否则用安装器，并说明原因；
 * - 需要安装器却没有适合本机的 → null（发版流水线还没传完）。
 */
export function chooseUpdate<A extends { name: string }>(
  assets: A[],
  installer: A | undefined,
  facts: { latestVersion: string; contract: number; backendHash: string | null },
): UpdateChoice<A> | null {
  let fullReason: UpdateChoice<A>['fullReason'] = 'no-renderer';
  for (const asset of assets) {
    const info = parseRendererAsset(asset.name);
    if (!info || info.version !== facts.latestVersion) continue;
    if (info.contract > facts.contract) {
      fullReason = 'shell';
      continue;
    }
    if (!sameBackend(info.backendHash, facts.backendHash)) {
      // 外壳也要换时以外壳为准；两者都不是才记成引擎
      if (fullReason !== 'shell') fullReason = 'engine';
      continue;
    }
    return { kind: 'hot', asset, renderer: info };
  }
  return installer ? { kind: 'full', asset: installer, fullReason } : null;
}
