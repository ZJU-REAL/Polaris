/* ============================================================
   构建期脚本：下载 uv 单文件二进制，放进 resources/uv/ 供 electron-builder
   以 extraResources 打进安装包（见 electron-builder.yml）。

   为什么打 uv 而不是打 Python：uv 一个静态二进制就能在首启时
   `uv python install` 出一套托管 Python 并装好后端依赖（ComfyUI 模式），
   安装包体积只多 uv 本身；打完整 Python + site-packages 则轻松破百兆，
   还要为三平台分别处理动态库。运行时引导逻辑在 src/main/engine-bootstrap.ts。

   版本钉死在常量里：uv 的行为（托管 Python 目录布局、venv 结构）是引导
   逻辑的隐含契约，随手升版可能悄悄改变布局；升级必须连同 engine-bootstrap
   一起验证后再改这里。运行时哨兵靠 resources/uv/version.txt 感知 uv 升级。

   macOS 特殊：桌面端发 --universal 单包，而 uv 的 release 是分架构的，
   所以 darwin 下载两个架构再 lipo 合成 universal——Intel 用户跑 arm64 uv
   会直接 exec format error，不能只打宿主架构。

   缓存：下载解包结果按 版本/目标三元组 存 .cache/uv/，重跑零下载。
   ============================================================ */

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/** 钉死的 uv 版本。升级前先本机跑通 dist + bootstrap 冒烟再改。 */
const UV_VERSION = '0.12.9';

const desktopDir = dirname(dirname(fileURLToPath(import.meta.url)));
const cacheDir = join(desktopDir, '.cache', 'uv', UV_VERSION);
const outDir = join(desktopDir, 'resources', 'uv');

/** platform/arch → uv release 的目标三元组。只列桌面端会发布的组合。 */
function targetsFor(platform, arch) {
  if (platform === 'darwin') {
    // universal 包：两个架构都要，之后 lipo 合并
    return ['aarch64-apple-darwin', 'x86_64-apple-darwin'];
  }
  const cpu = { x64: 'x86_64', arm64: 'aarch64' }[arch];
  if (!cpu) throw new Error(`fetch-uv: 不支持的架构 ${arch}`);
  if (platform === 'win32') return [`${cpu}-pc-windows-msvc`];
  if (platform === 'linux') return [`${cpu}-unknown-linux-gnu`];
  throw new Error(`fetch-uv: 不支持的平台 ${platform}`);
}

function assetName(target) {
  // windows 发 zip，其余是 tar.gz；两者 bsdtar（mac / win10+ / CI）都能解
  return target.includes('windows') ? `uv-${target}.zip` : `uv-${target}.tar.gz`;
}

async function download(url, dest) {
  console.log(`  downloading ${url}`);
  const res = await fetch(url, { redirect: 'follow' });
  if (!res.ok) throw new Error(`fetch-uv: 下载失败 ${res.status} ${url}`);
  const buf = Buffer.from(await res.arrayBuffer());
  mkdirSync(dirname(dest), { recursive: true });
  writeFileSync(dest, buf);
  return buf;
}

/** 解包后在目录树里找 uv 可执行文件（tar.gz 里有一层目录，zip 没有）。 */
function findBinary(root, name) {
  for (const entry of readdirSync(root)) {
    const p = join(root, entry);
    if (statSync(p).isDirectory()) {
      const found = findBinary(p, name);
      if (found) return found;
    } else if (entry === name) {
      return p;
    }
  }
  return null;
}

/** 取单个目标的 uv 二进制（带缓存），返回缓存里的可执行文件路径。 */
async function fetchTarget(target) {
  const binName = target.includes('windows') ? 'uv.exe' : 'uv';
  const cached = join(cacheDir, target, binName);
  if (existsSync(cached)) {
    console.log(`  cache hit  ${target}`);
    return cached;
  }
  const asset = assetName(target);
  // uv 的 release tag 没有 v 前缀（0.12.9，不是 v0.12.9）
  const url = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${asset}`;
  const archive = join(cacheDir, target, asset);
  const buf = await download(url, archive);

  // 校验 sha256：发布资产旁边都带 .sha256 文件（格式：`<hex>  <name>`）
  const shaRes = await fetch(`${url}.sha256`, { redirect: 'follow' });
  if (shaRes.ok) {
    const expected = (await shaRes.text()).trim().split(/\s+/)[0];
    const actual = createHash('sha256').update(buf).digest('hex');
    if (expected !== actual) {
      throw new Error(`fetch-uv: ${asset} 校验失败 expected=${expected} actual=${actual}`);
    }
  } else {
    console.warn(`  警告：取不到 ${asset}.sha256（${shaRes.status}），跳过校验`);
  }

  const extractDir = join(cacheDir, target, 'extract');
  rmSync(extractDir, { recursive: true, force: true });
  mkdirSync(extractDir, { recursive: true });
  if (process.platform === 'win32' && archive.endsWith('.zip')) {
    // CI 的 PATH 上 MSYS tar 先于系统 bsdtar，解 zip 直接 exit 128——
    // 用 Expand-Archive 绕开 tar 选型问题（win10+ 恒有）。
    execFileSync(
      'powershell',
      ['-NoProfile', '-Command', `Expand-Archive -Path '${archive}' -DestinationPath '${extractDir}' -Force`],
      { stdio: 'inherit' },
    );
  } else {
    execFileSync('tar', ['-xf', archive, '-C', extractDir], { stdio: 'inherit' });
  }
  const bin = findBinary(extractDir, binName);
  if (!bin) throw new Error(`fetch-uv: 解包后找不到 ${binName}（${asset}）`);
  cpSync(bin, cached);
  rmSync(extractDir, { recursive: true, force: true });
  rmSync(archive, { force: true });
  console.log(`  fetched    ${target}`);
  return cached;
}

async function main() {
  console.log(`fetch-uv: uv ${UV_VERSION} for ${process.platform}/${process.arch}`);
  const targets = targetsFor(process.platform, process.arch);
  const bins = [];
  for (const target of targets) bins.push(await fetchTarget(target));

  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  const outName = process.platform === 'win32' ? 'uv.exe' : 'uv';
  const outBin = join(outDir, outName);

  if (process.platform === 'darwin') {
    // 合成 universal：分架构的两个 Mach-O lipo 到一个文件
    execFileSync('lipo', ['-create', ...bins, '-output', outBin], { stdio: 'inherit' });
  } else {
    cpSync(bins[0], outBin);
  }
  if (process.platform !== 'win32') execFileSync('chmod', ['755', outBin]);

  // 运行时哨兵靠它感知 uv 升级（engine-bootstrap 读这个文件而不是执行
  // uv --version：读文件更快，也不会因二进制损坏而挂在引导之前）
  writeFileSync(join(outDir, 'version.txt'), `${UV_VERSION}\n`);
  const size = statSync(outBin).size;
  console.log(`fetch-uv: done → ${outBin} (${(size / 1024 / 1024).toFixed(1)} MB)`);
}

await main();
