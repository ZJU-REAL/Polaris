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
import { installedBackendHash, sameBackend } from './engine-compat';
import { extractTarGz } from './tar';
import { stagedSupersedes } from './version';

/** 更新包里必须带的元信息，解包后校验。 */
export interface RendererMeta {
  version: string;
  /** 这份界面要求的 IPC 契约版本；高于当前外壳就不能热更。 */
  contract: number;
  /** 这份界面是配哪份引擎打出来的（后端内容哈希）；与本机已装的不同就不能用。 */
  backendHash: string;
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
  return activePointer()?.dir ?? null;
}

/**
 * 真正在用的那份热更新界面；任何一条不满足都不用它、回落包内自带的界面：
 * - 契约版本高于外壳（外壳被降级过）；
 * - 不比外壳自带的新（整包更新后包内界面已经反超）——就地清掉；
 * - 不是配本机这份引擎打的。没带后端哈希的旧记录同样不认：那是只看契约版本
 *   的旧逻辑留下的，正是「新界面跑在旧引擎上」的来源。
 */
function activePointer(): Pointer | null {
  const p = readPointer();
  if (!p) return null;
  if (p.contract > CONTRACT_VERSION) return null;
  if (!stagedSupersedes(p.version, app.getVersion())) {
    clearStagedRenderer();
    return null;
  }
  if (!sameBackend(p.backendHash, installedBackendHash())) {
    clearStagedRenderer();
    return null;
  }
  return p;
}

/**
 * 用户眼前这份界面的版本。更新检查必须用它而不是 `app.getVersion()`：
 * 热更新不换外壳，外壳版本装完新界面也不会变。只认真正在用的那份。
 */
export function effectiveVersion(): string {
  return activePointer()?.version ?? app.getVersion();
}

/** 解包并启用一份新界面。抛错即视为失败，调用方不应切换。 */
export function stageRenderer(archive: Buffer, expected: RendererMeta): void {
  const dir = join(baseDir(), expected.version);
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });

  extractTarGz(archive, dir);
  // 查文件系统而不是查返回的路径列表：条目名的形态（./ 前缀、子目录写法）
  // 取决于打包命令，不该让它决定更新包是否被接受。
  if (!existsSync(join(dir, 'index.html'))) {
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
  // 文件名里只有哈希前缀，包里是完整的：两者都要与本机已装的引擎对得上
  if (
    typeof meta.backendHash !== 'string' ||
    !sameBackend(meta.backendHash, expected.backendHash) ||
    !sameBackend(meta.backendHash, installedBackendHash())
  ) {
    rmSync(dir, { recursive: true, force: true });
    throw new Error('update package was built for a different engine than the one installed');
  }

  writeFileSync(
    pointerPath(),
    JSON.stringify({ version: meta.version, contract: meta.contract, backendHash: meta.backendHash, dir }),
    'utf8',
  );
}

/** 回滚到包内自带的界面。加载失败时由 window.ts 调用。 */
export function clearStagedRenderer(): void {
  try {
    rmSync(pointerPath(), { force: true });
  } catch {
    /* 删不掉也不该阻断启动 */
  }
}
