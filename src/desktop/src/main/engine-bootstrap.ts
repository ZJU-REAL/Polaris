/* ============================================================
   首启引导：用安装包里自带的 uv，在 userData 下装出一套自足的 Python
   环境并给 legacy-engine 插件构造 command 模式配置（#607，ComfyUI 模式）。

   安装包只带两样东西（electron-builder extraResources）：
   - resources/uv/uv        钉版本的 uv 单文件二进制（fetch-uv.mjs 下载）
   - resources/backend/     后端源码 + 构建期算好的内容哈希 .hash（stage-backend.mjs）

   首次启动（或 uv/后端内容变化后）执行三步：
     uv python install 3.12  →  uv venv  →  uv pip install <resources/backend>
   全部产物落在 <dataDir>/engine/ 下（托管 Python、venv、uv 缓存、SQLite 库），
   卸载应用后删 userData 即彻底清干净，绝不污染系统 Python / 系统 uv。

   哨兵文件 engine/bootstrap.json 记录 {uv 版本, 后端哈希, Python 版本}：
   三者都没变就整段跳过（后续启动零开销）。哈希是构建期写死在包里的，
   运行时只读——不在每次启动时对上千个文件现算。

   本文件刻意 electron-free：所有路径由调用方传入。这让 CI 的装包冒烟
   （bootstrap-smoke.ts）能用 node 直接驱动同一段引导逻辑对着解包产物
   验证，而不必在无头环境里拉起整个 Electron。
   ============================================================ */

import { spawn } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { mkdir, rm } from 'node:fs/promises';
import { join } from 'node:path';

/** 引导要装的 Python 版本。与 backend pyproject 的 requires-python 对齐。 */
const PYTHON_VERSION = '3.12';

/** 本地引擎监听端口。与 legacy-engine 插件的默认端口保持一致。 */
export const ENGINE_PORT = 18080;

export type BootstrapPhase = 'check' | 'python' | 'venv' | 'install' | 'ready';

export interface BootstrapProgress {
  phase: BootstrapPhase;
  /** 子进程的一行输出（uv 的下载/安装进度都在 stderr 里）。 */
  line?: string;
}

export interface BootstrapOptions {
  /** 安装包资源目录（Electron 下是 process.resourcesPath）。 */
  resourcesDir: string;
  /** 可写数据根目录（Electron 下是 app.getPath('userData')）。 */
  dataDir: string;
  onProgress?: (p: BootstrapProgress) => void;
}

/** 引导产出：直接喂给 legacy-engine 插件的 command 模式配置。 */
export interface EngineCommand {
  mode: 'command';
  command: string[];
  port: number;
}

interface Sentinel {
  uvVersion: string;
  backendHash: string;
  pythonVersion: string;
}

function readTrimmed(path: string): string {
  return readFileSync(path, 'utf8').trim();
}

/** 跑一个 uv 子进程；输出逐行转发进度回调，非零退出带日志尾巴抛错。 */
function run(
  argv: string[],
  env: NodeJS.ProcessEnv,
  phase: BootstrapPhase,
  onProgress?: (p: BootstrapProgress) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(argv[0]!, argv.slice(1), { stdio: ['ignore', 'pipe', 'pipe'], env });
    const tail: string[] = [];
    const capture = (chunk: Buffer): void => {
      for (const line of chunk.toString().split('\n')) {
        if (!line.trim()) continue;
        tail.push(line);
        if (tail.length > 50) tail.shift();
        onProgress?.({ phase, line });
      }
    };
    child.stdout!.on('data', capture);
    child.stderr!.on('data', capture);
    child.on('error', reject);
    child.on('exit', (code, signal) => {
      if (code === 0) resolve();
      else {
        reject(
          new Error(
            `engine-bootstrap: ${argv.join(' ')} 失败 (code=${code}, signal=${signal})\n${tail.join('\n')}`,
          ),
        );
      }
    });
  });
}

/**
 * 引导（或复用）内嵌后端环境，返回 legacy-engine 的 command 配置。
 * 幂等：哨兵匹配时不产生任何子进程，只做几次文件读取。
 */
export async function bootstrapEngine(opts: BootstrapOptions): Promise<EngineCommand> {
  const { resourcesDir, dataDir, onProgress } = opts;

  const uvBin = join(resourcesDir, 'uv', process.platform === 'win32' ? 'uv.exe' : 'uv');
  const backendDir = join(resourcesDir, 'backend');
  for (const [what, path] of [
    ['uv 二进制', uvBin],
    ['后端源码', join(backendDir, 'pyproject.toml')],
    ['后端哈希', join(backendDir, '.hash')],
  ] as const) {
    if (!existsSync(path)) {
      throw new Error(`engine-bootstrap: 安装包里缺少${what}（${path}）——打包时没跑 stage:resources？`);
    }
  }
  const uvVersion = readTrimmed(join(resourcesDir, 'uv', 'version.txt'));
  const backendHash = readTrimmed(join(backendDir, '.hash'));

  const engineDir = join(dataDir, 'engine');
  const venvDir = join(engineDir, 'venv');
  const venvPython =
    process.platform === 'win32'
      ? join(venvDir, 'Scripts', 'python.exe')
      : join(venvDir, 'bin', 'python');
  const sentinelPath = join(engineDir, 'bootstrap.json');

  onProgress?.({ phase: 'check' });
  const wanted: Sentinel = { uvVersion, backendHash, pythonVersion: PYTHON_VERSION };
  let fresh = true;
  try {
    const current = JSON.parse(readFileSync(sentinelPath, 'utf8')) as Partial<Sentinel>;
    fresh = !(
      current.uvVersion === wanted.uvVersion &&
      current.backendHash === wanted.backendHash &&
      current.pythonVersion === wanted.pythonVersion &&
      existsSync(venvPython)
    );
  } catch {
    /* 哨兵不存在或损坏 → 全量引导 */
  }

  if (fresh) {
    await mkdir(engineDir, { recursive: true });
    // uv 的一切可变状态都圈进 engine/：托管 Python、缓存都不落用户主目录，
    // UV_NO_CONFIG 再挡掉用户自己的 uv.toml（比如镜像源/固定 python 目录），
    // only-managed 保证绝不用系统里碰巧存在的 Python——机器上有没有 Python
    // 结果必须一致。
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      UV_PYTHON_INSTALL_DIR: join(engineDir, 'python'),
      UV_CACHE_DIR: join(engineDir, 'uv-cache'),
      UV_PYTHON_PREFERENCE: 'only-managed',
      UV_NO_CONFIG: '1',
    };
    delete env.VIRTUAL_ENV; // 开发者 shell 里的 venv 不能泄漏进来

    onProgress?.({ phase: 'python' });
    await run([uvBin, 'python', 'install', PYTHON_VERSION], env, 'python', onProgress);

    onProgress?.({ phase: 'venv' });
    // 重建而不复用：哨兵不匹配意味着依赖集合可能变了，增量升级一个旧 venv
    // 会攒出「卸载不净」的幽灵依赖，全量重来才可复现
    await rm(venvDir, { recursive: true, force: true });
    await run([uvBin, 'venv', '--python', PYTHON_VERSION, venvDir], env, 'venv', onProgress);

    onProgress?.({ phase: 'install' });
    await run([uvBin, 'pip', 'install', '--python', venvPython, backendDir], env, 'install', onProgress);

    writeFileSync(sentinelPath, `${JSON.stringify(wanted, null, 2)}\n`);
  }

  onProgress?.({ phase: 'ready' });
  return { mode: 'command', command: buildEngineCommand(venvPython, backendDir, engineDir), port: ENGINE_PORT };
}

/**
 * 引擎启动 argv：与 legacy-engine docker 模式的 `sh -lc "alembic && uvicorn"`
 * 同构，但 command 模式没有 shell，所以用 python -c 的小启动器串联两步。
 * 启动器同时负责 cwd 与数据库地址：
 * - chdir 到后端源码目录——alembic.ini 的 script_location/prepend_sys_path
 *   都是相对 cwd 的相对路径；
 * - POLARIS_DATABASE_URL 指到 userData 的 SQLite 文件（legacy-engine 只注入
 *   POLARIS_PROFILE，数据库路径是引导方才知道的信息，所以写在启动器里）。
 */
function buildEngineCommand(venvPython: string, backendDir: string, engineDir: string): string[] {
  const dbPath = join(engineDir, 'polaris.db').split('\\').join('/');
  // JSON.stringify 产出的字符串字面量对 Python 同样合法（转义子集兼容），
  // 借它安全嵌入含空格/反斜杠/非 ASCII 的路径
  const launcher = [
    'import os, subprocess, sys',
    "os.environ.setdefault('POLARIS_PROFILE', 'desktop')",
    `os.environ['POLARIS_DATABASE_URL'] = ${JSON.stringify(`sqlite+aiosqlite:///${dbPath}`)}`,
    `os.chdir(${JSON.stringify(backendDir)})`,
    "subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], check=True)",
    'import uvicorn',
    `uvicorn.run('app.main:app', host='127.0.0.1', port=${ENGINE_PORT})`,
  ].join('\n');
  return [venvPython, '-c', launcher];
}
