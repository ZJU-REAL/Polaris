/* ============================================================
   热更新的界面层：把新版界面放进 userData，由 app:// 协议优先提供。

   为什么这条路走得通，而整包自动更新走不通：界面本来就是一堆静态文件，我们用
   自己的协议处理器提供，换一份再 reload 就完成了更新——**完全不碰已签名的
   app bundle**。macOS 的整包自动更新要求 Developer ID 签名（Squirrel.Mac 会校验
   下载包的签名并与运行中的 app 比对，ad-hoc 一律拒绝），而热更新绕开了这条限制。

   代价与边界：
   - 只能更新界面。preload、主进程、Electron 本身变了都必须走安装器，判据是
     contract 版本（见 index.ts 的 canHotUpdate）。
   - 热更新等于执行下载来的 JS，绕过了操作系统的代码签名校验。信任锚点是
     HTTPS + GitHub Releases，且必须由用户在弹窗里显式确认。
   - 加载失败要能退回：pointer 写在单独的文件里，回滚只是删掉它。
   ============================================================ */

import { app } from 'electron';
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import { CONTRACT_VERSION } from '../../shared/contract';
import { extractTarGz } from './tar';

/** 更新包里必须带的元信息，解包后校验。 */
export interface RendererMeta {
  version: string;
  /** 这份界面要求的 IPC 契约版本；高于当前外壳就不能热更。 */
  contract: number;
}

interface Pointer extends RendererMeta {
  dir: string;
}

function baseDir(): string {
  return join(app.getPath('userData'), 'renderer');
}

function pointerPath(): string {
  return join(baseDir(), 'active.json');
}

function readPointer(): Pointer | null {
  try {
    const p = JSON.parse(readFileSync(pointerPath(), 'utf8')) as Pointer;
    return p && typeof p.dir === 'string' && existsSync(join(p.dir, 'index.html')) ? p : null;
  } catch {
    return null;
  }
}

/**
 * 当前应当提供的界面目录；没有可用的热更新版本时返回 null，调用方回落到包内资源。
 * 契约版本在这里再查一次：外壳可能被整包更新降级过，留着的旧 pointer 不能生效。
 */
export function stagedRendererDir(): string | null {
  const p = readPointer();
  if (!p) return null;
  if (p.contract > CONTRACT_VERSION) return null;
  return p.dir;
}

export function stagedVersion(): string | null {
  return readPointer()?.version ?? null;
}

/** 解包并启用一份新界面。抛错即视为失败，调用方不应切换。 */
export function stageRenderer(archive: Buffer, expected: RendererMeta): void {
  const dir = join(baseDir(), expected.version);
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });

  const files = extractTarGz(archive, dir);
  if (!files.includes('index.html')) {
    rmSync(dir, { recursive: true, force: true });
    throw new Error('update package has no index.html');
  }

  // 包里自带的 meta 必须与 release 上声明的一致，否则说明拿错了包
  let meta: RendererMeta;
  try {
    meta = JSON.parse(readFileSync(join(dir, 'meta.json'), 'utf8')) as RendererMeta;
  } catch {
    rmSync(dir, { recursive: true, force: true });
    throw new Error('update package has no meta.json');
  }
  if (meta.version !== expected.version || meta.contract !== expected.contract) {
    rmSync(dir, { recursive: true, force: true });
    throw new Error('update package metadata does not match the release');
  }

  writeFileSync(pointerPath(), JSON.stringify({ ...meta, dir }), 'utf8');
}

/** 回滚到包内自带的界面。加载失败时由 window.ts 调用。 */
export function clearStagedRenderer(): void {
  try {
    rmSync(pointerPath(), { force: true });
  } catch {
    /* 删不掉也不该阻断启动 */
  }
}
