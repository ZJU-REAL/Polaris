/* ============================================================
   更新检查与应用。

   两条路径，优先热更：
   - **热更新**：界面层单独打成 renderer-<version>-c<contract>.tar.gz。契约版本
     不高于当前外壳时，下载→解包→reload，**不重启、不碰 app bundle**。
   - **整包更新**：preload/主进程/Electron 变了（契约升级或没有界面包）时，只能
     下载安装器。Windows 直接拉起安装器；macOS 只能打开 dmg 让用户拖进
     Applications——Squirrel.Mac 要求 Developer ID 签名才肯静默替换，本项目是
     ad-hoc 签名，这条绕不过。

   契约版本作判据是复用现成机制：CONTRACT_VERSION 本来就是 preload 与界面之间
   的约定版本，界面要求的版本一旦高于外壳，说明桥接层也变了，热更就不安全。
   ============================================================ */

import { app, net, shell } from 'electron';
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import { CONTRACT_VERSION, type UpdateInfo } from '../../shared/contract';
import { fail, finish, log, progress, startJob } from '../ipc/events';
import { getWindow } from '../window';
import { effectiveVersion, stageRenderer } from './renderer-store';
import { compareVersions } from './version';

const REPO = 'ZJU-REAL/Polaris';
const RELEASES_URL = `https://api.github.com/repos/${REPO}/releases/latest`;
const RENDERER_RE = /^renderer-(.+)-c(\d+)\.tar\.gz$/;

interface Asset {
  name: string;
  browser_download_url: string;
  size: number;
}

interface Release {
  tag_name: string;
  name: string;
  body: string;
  published_at: string;
  assets: Asset[];
}

function installerFor(assets: Asset[]): Asset | undefined {
  const match = (re: RegExp) => assets.find((a) => re.test(a.name));
  if (process.platform === 'darwin') return match(/universal\.dmg$/) ?? match(/\.dmg$/);
  if (process.platform === 'win32') return match(/\.exe$/) ?? match(/-win\.zip$/);
  return match(/\.AppImage$/) ?? match(/\.deb$/);
}

let cached: UpdateInfo | null = null;

export async function checkForUpdate(): Promise<UpdateInfo> {
  const currentVersion = effectiveVersion();
  const base: UpdateInfo = { available: false, currentVersion };
  try {
    const res = await net.fetch(RELEASES_URL, {
      headers: { Accept: 'application/vnd.github+json' },
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) return base;
    const release = (await res.json()) as Release;
    const latestVersion = (release.tag_name ?? '').replace(/^v/, '');
    if (!latestVersion || compareVersions(latestVersion, currentVersion) <= 0) return base;

    const rendererAsset = release.assets.find((a) => {
      const m = RENDERER_RE.exec(a.name);
      return m?.[1] === latestVersion && Number(m[2]) <= CONTRACT_VERSION;
    });
    const installer = installerFor(release.assets);
    const asset = rendererAsset ?? installer;
    if (!asset) return base;

    cached = {
      available: true,
      currentVersion,
      latestVersion,
      notes: release.body ?? '',
      publishedAt: release.published_at,
      kind: rendererAsset ? 'hot' : 'full',
      contract: rendererAsset ? Number(RENDERER_RE.exec(rendererAsset.name)![2]) : CONTRACT_VERSION,
      downloadUrl: asset.browser_download_url,
      downloadSize: asset.size,
      installerUrl: installer?.browser_download_url,
    };
    return cached;
  } catch {
    // 检查更新失败不该打扰用户：当作没有更新
    return base;
  }
}

async function download(url: string, jobId: string, total: number): Promise<Buffer> {
  const res = await net.fetch(url, { signal: AbortSignal.timeout(300_000) });
  if (!res.ok || !res.body) throw new Error(`download failed: HTTP ${res.status}`);
  const chunks: Uint8Array[] = [];
  let received = 0;
  const reader = res.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    progress(jobId, 'download', received, total || received);
  }
  return Buffer.concat(chunks);
}

/**
 * 下载并应用更新。立即返回 jobId，进度与结果经 job.* 事件推送。
 * 热更成功时结果里带 reloaded: true，界面已经是新的，不需要重启。
 */
export function applyUpdate(): { jobId: string } {
  const info = cached;
  const jobId = startJob('update');
  if (!info?.available || !info.downloadUrl) {
    fail(jobId, 'ERR_NO_UPDATE', 'no update available; run a check first');
    return { jobId };
  }

  void (async () => {
    try {
      const data = await download(info.downloadUrl!, jobId, info.downloadSize ?? 0);

      let payload = data;
      if (info.kind === 'hot') {
        try {
          log(jobId, 'installing renderer update');
          stageRenderer(data, { version: info.latestVersion!, contract: info.contract! });
          finish(jobId, { kind: 'hot', reloaded: true, version: info.latestVersion });
          // 换界面即完成更新：重新加载窗口就跑在新版本上了
          getWindow()?.webContents.reload();
          return;
        } catch (err) {
          // 界面包装不上就退回整包更新，别让用户卡在「更新失败」上。坏包、旧客户端
          // 的解包器有 bug、契约声明与实际内容不符——都归到这条路上。
          if (!info.installerUrl) throw err;
          log(jobId, `renderer update failed (${String(err)}), falling back to installer`);
          payload = await download(info.installerUrl, jobId, 0);
        }
      }

      const dir = join(app.getPath('userData'), 'updates');
      mkdirSync(dir, { recursive: true });
      const source = payload === data ? info.downloadUrl! : info.installerUrl!;
      const file = join(dir, source.split('/').pop() ?? 'update');
      writeFileSync(file, payload);
      finish(jobId, { kind: 'full', reloaded: false, path: file, version: info.latestVersion });

      if (process.platform === 'win32') {
        // NSIS 安装器不要求签名，能自己完成替换；detached 是为了让它活过本进程退出
        spawn(file, [], { detached: true, stdio: 'ignore' }).unref();
        app.quit();
      } else {
        // macOS 只能把 dmg 打开让用户拖进 Applications（无 Developer ID 签名，
        // 静默替换会被系统拒绝）；Linux 交给文件管理器。
        await shell.openPath(file);
      }
    } catch (err) {
      fail(jobId, 'ERR_UPDATE_FAILED', err instanceof Error ? err.message : String(err));
    }
  })();

  return { jobId };
}
