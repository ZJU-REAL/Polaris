/* ============================================================
   构建期脚本：把后端源码整理到 resources/backend/，供 electron-builder
   以 extraResources 打进安装包。

   为什么先「staging」而不是让 electron-builder 直接从 ../backend 过滤拷贝：
   1. 内容哈希必须在构建期算好写成 resources/backend/.hash——运行时哨兵
      （engine-bootstrap）只读这个文件来判断「后端变了没、要不要重装 venv」。
      运行时对着上千个文件现算哈希，白白拖慢每次冷启动。
   2. 白名单拷贝比 electron-builder 的 glob 过滤器可控：打进去的就是
      运行需要的那几样，不会因为 backend 目录里多了个临时文件而进包。

   打「源码目录 + uv pip install <dir>」而不是预构建 sdist/wheel：
   alembic/ 与 alembic.ini 不在 Python 包内（setuptools 只收 app* 与 worker*），
   运行迁移必须有源码目录做 cwd；既然目录反正要进包，直接对目录 install
   （uv 会就地构建）比再维护一份 sdist 少一条产物链。
   ============================================================ */

import { createHash } from 'node:crypto';
import { copyFileSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const desktopDir = dirname(dirname(fileURLToPath(import.meta.url)));
const backendDir = join(dirname(desktopDir), 'backend');
const outDir = join(desktopDir, 'resources', 'backend');

/** 进包的顶层条目（白名单）。tests/ 与开发期杂物永不进包。 */
const INCLUDE = ['app', 'worker', 'alembic', 'alembic.ini', 'pyproject.toml'];

/** 递归拷贝时按名字排除的目录/文件。 */
const EXCLUDE_NAMES = new Set(['__pycache__', '.pytest_cache', '.ruff_cache', '.venv', 'build']);

function shouldSkip(name) {
  return (
    EXCLUDE_NAMES.has(name) ||
    name.endsWith('.pyc') ||
    name.endsWith('.egg-info') ||
    name === '.DS_Store'
  );
}

function copyTree(src, dest, collected) {
  const st = statSync(src);
  if (st.isDirectory()) {
    mkdirSync(dest, { recursive: true });
    for (const entry of readdirSync(src).sort()) {
      if (shouldSkip(entry)) continue;
      copyTree(join(src, entry), join(dest, entry), collected);
    }
  } else {
    copyFileSync(src, dest);
    collected.push(dest);
  }
}

function main() {
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });

  const files = [];
  for (const entry of INCLUDE) {
    copyTree(join(backendDir, entry), join(outDir, entry), files);
  }

  // 内容哈希 = 排序后的 (相对路径 + 文件内容) 流式 sha256。路径参与哈希：
  // 只挪文件不改内容同样要触发重装（import 路径可能因此变化）。
  //
  // 这个哈希还有第二个用途：界面包声明「我是配哪份引擎打的」（见
  // src/main/updates/engine-compat.ts），客户端拿它与本机的 .hash 比。界面包在
  // Linux 上打、安装包在三个平台各打一份，所以同一份源码在三个平台必须算出
  // 同一个值，两处要对齐：
  // - 排序按「/ 分隔的相对路径」排，而不是按本机的绝对路径排（Windows 的 \ 排在
  //   数字和大写字母之后，/ 排在之前，同一批文件两边顺序会不一样）；
  // - 文本文件按 LF 算（Windows 上 git 检出默认是 CRLF）。
  // 在 macOS / Linux 上，这两处都不改变原来的结果。
  const entries = files
    .map((file) => ({ file, rel: relative(outDir, file).split('\\').join('/') }))
    .sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
  const hash = createHash('sha256');
  for (const { file, rel } of entries) {
    hash.update(rel);
    hash.update('\0');
    hash.update(contentForHash(file));
    hash.update('\0');
  }
  const digest = hash.digest('hex');
  writeFileSync(join(outDir, '.hash'), `${digest}\n`);

  // 引擎自报的版本（/api/health、设置 → 关于、反馈里的版本号）。以前写死在源码里，
  // 每个发行版都自称 0.1.0。版本取桌面端 package.json——发版流水线在这一步之前
  // 已经按 tag 把它改好了。
  // 写在算完哈希之后、且不参与哈希：哈希回答的是「引擎代码变了没有」，只改版本号
  // 的发布不该让每个用户重装一遍环境，也不该挡住只换界面的更新。
  const version = releaseVersion();
  writeFileSync(
    join(outDir, 'app', '_release.py'),
    [
      '"""Written by the desktop packaging step (src/desktop/scripts/stage-backend.mjs). Do not edit."""',
      '',
      `VERSION = ${JSON.stringify(version)}`,
      `BACKEND_HASH = ${JSON.stringify(digest)}`,
      '',
    ].join('\n'),
  );

  const total = files.reduce((sum, f) => sum + statSync(f).size, 0);
  console.log(
    `stage-backend: ${files.length} files, ${(total / 1024 / 1024).toFixed(1)} MB → ${outDir}`,
  );
  console.log(`stage-backend: hash ${digest}`);
  console.log(`stage-backend: engine version ${version}`);
}

/** 按文本处理（换行归一成 LF 再算哈希）的扩展名。其余按原始字节。 */
const TEXT_EXTENSIONS = new Set([
  'py', 'pyi', 'toml', 'ini', 'cfg', 'mako', 'yaml', 'yml', 'json', 'txt', 'md',
  'tex', 'sty', 'cls', 'bib', 'bst', 'html', 'htm', 'css', 'js', 'j2', 'jinja', 'sql', 'csv',
]);

function contentForHash(file) {
  const data = readFileSync(file);
  const ext = file.includes('.') ? file.slice(file.lastIndexOf('.') + 1).toLowerCase() : '';
  if (!TEXT_EXTENSIONS.has(ext) || !data.includes(13)) return data;
  return Buffer.from(data.toString('latin1').replace(/\r\n/g, '\n'), 'latin1');
}

/** 这次打包的版本号：POLARIS_RELEASE_VERSION 优先，否则读桌面端 package.json。 */
function releaseVersion() {
  const fromEnv = process.env.POLARIS_RELEASE_VERSION?.trim().replace(/^v/, '');
  if (fromEnv) return fromEnv;
  const pkg = JSON.parse(readFileSync(join(desktopDir, 'package.json'), 'utf8'));
  return String(pkg.version);
}

main();
