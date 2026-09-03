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
  const hash = createHash('sha256');
  for (const file of files.sort()) {
    hash.update(relative(outDir, file).split('\\').join('/'));
    hash.update('\0');
    hash.update(readFileSync(file));
    hash.update('\0');
  }
  const digest = hash.digest('hex');
  writeFileSync(join(outDir, '.hash'), `${digest}\n`);

  const total = files.reduce((sum, f) => sum + statSync(f).size, 0);
  console.log(
    `stage-backend: ${files.length} files, ${(total / 1024 / 1024).toFixed(1)} MB → ${outDir}`,
  );
  console.log(`stage-backend: hash ${digest}`);
}

main();
