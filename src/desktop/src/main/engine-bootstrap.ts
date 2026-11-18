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

   网络不好时怎么办（下载 Python 与装依赖两步要联网）：
   - 下载源按顺序试：先按用户自己的环境来（环境变量、代理、uv.toml 都照常
     生效），不行换国内镜像（PyPI 走清华源，Python 走 npmmirror 的
     python-build-standalone 镜像），用户配置本身可能就是问题所在时再试一次
     「不读用户配置的官方源」。哪个源成了，后面的步骤和下次升级就先用它。
   - 每一步有总时限，长时间既没输出也没落盘就判定卡死，杀掉换下一个源。
   - 代理：调用方给的优先（设置里填的 / 系统代理），否则沿用环境变量。
   - 开工前看一眼磁盘，不够直接说，不等装到一半才报一句看不懂的错。

   升级不动旧环境：新环境装在 venv.next 里，装好、验过才和 venv 对调；
   中途失败（断网、磁盘满、用户退出）旧环境原样还在，这次启动继续用它，
   下次启动再试。

   可取消：调用方给 AbortSignal，退出应用时正在跑的 uv 连同它的子进程一起
   收掉；留下的 venv.next 下次启动时清掉重来，已下载的包在 uv 缓存里不用重下。

   本文件刻意 electron-free：所有路径由调用方传入。这让 CI 的装包冒烟
   （bootstrap-smoke.ts）能用 node 直接驱动同一段引导逻辑对着解包产物
   验证，而不必在无头环境里拉起整个 Electron。
   ============================================================ */

import { execFile, spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { mkdir, rename, rm, statfs, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

import { dirSize } from './bootstrap-progress';

/** 引导要装的 Python 版本。与 backend pyproject 的 requires-python 对齐。 */
const PYTHON_VERSION = '3.12';

/** 本地引擎监听端口。与 legacy-engine 插件的默认端口保持一致。 */
export const ENGINE_PORT = 18080;

/** 国内镜像：PyPI（清华）与托管 Python 的下载地址（npmmirror，保留历史版本）。 */
export const PYPI_MIRROR = 'https://pypi.tuna.tsinghua.edu.cn/simple';
export const PYTHON_MIRROR = 'https://registry.npmmirror.com/-/binary/python-build-standalone';

/**
 * 开工前要求的空闲空间。实测一套环境约 0.85 GB（托管 Python 72 MB、uv 缓存
 * 370 MB、venv 390 MB）；升级时新旧 venv 并存，再留些余量。
 */
export const MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024;

export type BootstrapPhase = 'check' | 'python' | 'venv' | 'install' | 'ready';

/** 下载源：default=照用户环境来；mirror=国内镜像；official=不读用户配置的官方源。 */
export type SourceId = 'default' | 'mirror' | 'official';

/** 镜像策略：auto=官方不通再换镜像；on=先用镜像；off=不用镜像。 */
export type MirrorMode = 'auto' | 'on' | 'off';

export interface BootstrapNetwork {
  /** 代理地址（http://host:port、socks5://host:port）。空 = 沿用环境变量。 */
  proxy?: string | null;
  mirror?: MirrorMode;
}

export interface BootstrapProgress {
  phase: BootstrapPhase;
  /** 子进程的一行输出（uv 的下载/安装进度都在 stderr 里），或引导自己的说明（[polaris] 开头）。 */
  line?: string;
}

/** 各步骤的时限（毫秒）。默认值见 DEFAULT_TIMEOUTS，测试可调小。 */
export interface StepTimeouts {
  python: number;
  venv: number;
  install: number;
  /** 既没有输出也没有落盘增长超过这么久，判定卡死。 */
  stall: number;
}

export const DEFAULT_TIMEOUTS: StepTimeouts = {
  python: 15 * 60_000,
  venv: 3 * 60_000,
  install: 45 * 60_000,
  stall: 10 * 60_000,
};

export interface BootstrapOptions {
  /** 安装包资源目录（Electron 下是 process.resourcesPath）。 */
  resourcesDir: string;
  /** 可写数据根目录（Electron 下是 app.getPath('userData')）。 */
  dataDir: string;
  onProgress?: (p: BootstrapProgress) => void;
  /** 取消引导（退出应用）。取消后抛 BootstrapError('cancelled')。 */
  signal?: AbortSignal;
  network?: BootstrapNetwork;
  /** 引擎监听端口，默认 ENGINE_PORT。只给冒烟/调试用，安装包不改。 */
  port?: number;
  /** 子进程环境的底子，默认 process.env。 */
  env?: NodeJS.ProcessEnv;
  timeouts?: Partial<StepTimeouts>;
  /** 开工前要求的空闲字节数，默认 MIN_FREE_BYTES；0 = 不检查。 */
  minFreeBytes?: number;
  /** 每个源各试几轮，默认 2。 */
  rounds?: number;
  /** 换源/重试之间的等待，默认 2 秒起、逐轮加长。 */
  retryDelayMs?: number;
}

/** 一步是怎么成的：用了哪个源、试了几次、花了多久。给日志、诊断信息和冒烟用。 */
export interface StepReport {
  phase: BootstrapPhase;
  source: SourceId;
  attempts: number;
  ms: number;
}

/** 引导产出：直接喂给 legacy-engine 插件的 command 模式配置。 */
export interface EngineCommand {
  mode: 'command';
  command: string[];
  port: number;
  /** 这次真的装了环境时，每一步的记录；复用现成环境时为空。 */
  steps: StepReport[];
  /**
   * 新环境没装成、这次启动继续用上一套环境。引擎能起来，但依赖可能与新版代码
   * 不完全匹配；下次启动会再试。
   */
  degraded?: { reason: string };
}

export type BootstrapErrorCode = 'resources' | 'disk-space' | 'cancelled' | 'download' | 'install';

/** 引导失败。code 给界面挑文案用，message 是给日志看的英文原因。 */
export class BootstrapError extends Error {
  constructor(
    readonly code: BootstrapErrorCode,
    message: string,
    /** 失败步骤最后几行输出。 */
    readonly tail: string[] = [],
  ) {
    super(message);
    this.name = 'BootstrapError';
  }
}

interface Sentinel {
  uvVersion: string;
  backendHash: string;
  pythonVersion: string;
  /** 上次装成功用的下载源，下次升级先试它。 */
  source?: SourceId;
}

function readTrimmed(path: string): string {
  return readFileSync(path, 'utf8').trim();
}

/* ---------------- 下载源与环境 ---------------- */

/** 用户自己给 uv 配过下载源吗（环境变量或 uv.toml）。配过的话，失败时值得再试一次「不读配置」。 */
export function hasUserUvConfig(env: NodeJS.ProcessEnv, home: string = homedir()): boolean {
  for (const name of ['UV_DEFAULT_INDEX', 'UV_INDEX_URL', 'UV_INDEX', 'UV_EXTRA_INDEX_URL', 'UV_CONFIG_FILE', 'UV_PYTHON_INSTALL_MIRROR']) {
    if (env[name]) return true;
  }
  const candidates =
    process.platform === 'win32'
      ? [join(env.APPDATA ?? join(home, 'AppData', 'Roaming'), 'uv', 'uv.toml')]
      : [join(env.XDG_CONFIG_HOME || join(home, '.config'), 'uv', 'uv.toml'), '/etc/uv/uv.toml'];
  return candidates.some((path) => existsSync(path));
}

/**
 * 按镜像策略排出下载源的尝试顺序。lastGood 是上次装成功的源：auto 模式下
 * 把它提到最前——上次官方源不通的机器，这次多半还是不通。
 */
export function sourceOrder(mode: MirrorMode, userConfigured: boolean, lastGood?: SourceId): SourceId[] {
  let order: SourceId[];
  if (mode === 'on') order = ['mirror', 'default'];
  else if (mode === 'off') order = ['default'];
  else order = ['default', 'mirror'];
  // 没有用户配置时 official 与 default 是同一回事，不必多试一遍
  if (userConfigured) order.push('official');
  if (mode === 'auto' && lastGood && order.includes(lastGood)) {
    order = [lastGood, ...order.filter((s) => s !== lastGood)];
  }
  return order;
}

const INDEX_ENV = ['UV_DEFAULT_INDEX', 'UV_INDEX_URL', 'UV_INDEX', 'UV_EXTRA_INDEX_URL', 'UV_CONFIG_FILE', 'UV_PYTHON_INSTALL_MIRROR'];
const PROXY_ENV = ['HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY'];

/**
 * 某个下载源下 uv 子进程的环境。
 *
 * uv 的一切可变状态都圈进 engine/：托管 Python、缓存都不落用户主目录；
 * only-managed 保证绝不用系统里碰巧存在的 Python——机器上有没有 Python 结果
 * 必须一致。这几项由环境变量给出，优先级高于 uv.toml，所以 default 源可以放心
 * 读用户的 uv.toml：用户在那里配的镜像/代理正是他们的网络需要的东西（以前
 * 一律 UV_NO_CONFIG=1，等于把用户唯一的自救办法堵死）。镜像与官方两个后备源
 * 才关掉用户配置——走到那一步说明用户配置可能就是失败的原因。
 */
export function buildUvEnv(
  base: NodeJS.ProcessEnv,
  engineDir: string,
  source: SourceId,
  proxy?: string | null,
): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    ...base,
    UV_PYTHON_INSTALL_DIR: join(engineDir, 'python'),
    UV_CACHE_DIR: join(engineDir, 'uv-cache'),
    UV_PYTHON_PREFERENCE: 'only-managed',
    // Windows 上 Python 默认 cp1252，读 UTF-8 源码/配置直接 charmap 崩；
    // 统一强制 UTF-8 模式，其余平台本就是 UTF-8，无副作用
    PYTHONUTF8: '1',
  };
  delete env.VIRTUAL_ENV; // 开发者 shell 里的 venv 不能泄漏进来
  delete env.UV_NO_CONFIG;
  // 读超时默认 30 秒，慢网络上大包容易误判；用户自己设了就听用户的
  if (!env.UV_HTTP_TIMEOUT) env.UV_HTTP_TIMEOUT = '60';
  if (proxy) {
    for (const name of PROXY_ENV) {
      env[name] = proxy;
      env[name.toLowerCase()] = proxy;
    }
  }
  if (source !== 'default') {
    env.UV_NO_CONFIG = '1';
    for (const name of INDEX_ENV) delete env[name];
  }
  if (source === 'mirror') {
    env.UV_DEFAULT_INDEX = PYPI_MIRROR;
    env.UV_PYTHON_INSTALL_MIRROR = PYTHON_MIRROR;
  }
  return env;
}

function describeSource(source: SourceId): string {
  if (source === 'mirror') return `China mirrors (${new URL(PYPI_MIRROR).host}, ${new URL(PYTHON_MIRROR).host})`;
  if (source === 'official') return 'official sources, user uv config ignored';
  return 'default sources';
}

/* ---------------- 子进程 ---------------- */

/** 连同子进程一起杀：uv 会再起构建后端等子进程，只杀 uv 自己会留孤儿。 */
function killTree(pid: number | undefined, signal: NodeJS.Signals): void {
  if (pid === undefined) return;
  if (process.platform === 'win32') {
    execFile('taskkill', ['/pid', String(pid), '/T', '/F'], () => undefined);
    return;
  }
  try {
    process.kill(-pid, signal); // detached: true → 自成进程组
  } catch {
    try {
      process.kill(pid, signal);
    } catch {
      /* 已退出 */
    }
  }
}

interface RunOptions {
  env: NodeJS.ProcessEnv;
  phase: BootstrapPhase;
  onProgress?: (p: BootstrapProgress) => void;
  signal?: AbortSignal;
  timeoutMs: number;
  stallMs: number;
  /** 量「还在落盘吗」的目录；不给就只看输出。 */
  watchDir?: string;
}

/** 这一次运行为什么没成；调用方据此决定换源重试还是直接放弃。 */
class RunFailure extends Error {
  constructor(
    readonly reason: 'exit' | 'timeout' | 'stalled' | 'spawn' | 'cancelled',
    message: string,
    readonly tail: string[],
  ) {
    super(message);
  }
}

/** 跑一个 uv 子进程；输出逐行转发进度回调。超时、卡死、取消都会连进程树一起杀。 */
function run(argv: string[], opts: RunOptions): Promise<void> {
  const { env, phase, onProgress, signal, timeoutMs, stallMs, watchDir } = opts;
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new RunFailure('cancelled', 'cancelled', []));
      return;
    }
    const child = spawn(argv[0]!, argv.slice(1), {
      stdio: ['ignore', 'pipe', 'pipe'],
      env,
      detached: process.platform !== 'win32',
      windowsHide: true,
    });
    const tail: string[] = [];
    let lastActivity = Date.now();
    let killedFor: 'timeout' | 'stalled' | 'cancelled' | null = null;
    let settled = false;

    const emit = (line: string): void => {
      if (!line.trim()) return;
      lastActivity = Date.now();
      tail.push(line);
      if (tail.length > 50) tail.shift();
      onProgress?.({ phase, line });
    };
    // 每个流各留一段「半行」：data 块可能切在行中间，拼上下一块再按行发
    const capture = () => {
      let partial = '';
      const onData = (chunk: Buffer): void => {
        const parts = (partial + chunk.toString()).split('\n');
        partial = parts.pop() ?? '';
        for (const line of parts) emit(line);
      };
      const flush = (): void => {
        emit(partial);
        partial = '';
      };
      return { onData, flush };
    };
    const out = capture();
    const err = capture();
    child.stdout!.on('data', out.onData);
    child.stderr!.on('data', err.onData);

    const kill = (why: 'timeout' | 'stalled' | 'cancelled'): void => {
      if (killedFor) return;
      killedFor = why;
      killTree(child.pid, 'SIGTERM');
      // 不肯走的再补一刀；unref：进程已经没了就别拖住事件循环
      setTimeout(() => killTree(child.pid, 'SIGKILL'), 3_000).unref();
    };

    const overall = setTimeout(() => kill('timeout'), timeoutMs);
    // uv 在非 TTY 下下载大文件时可以很久不打一行字，所以「卡死」不能只看输出，
    // 还要看盘上有没有在长
    let lastSize = -1;
    let measuring = false;
    const watchdog = setInterval(() => {
      if (Date.now() - lastActivity > stallMs) {
        kill('stalled');
        return;
      }
      if (!watchDir || measuring) return;
      measuring = true;
      void dirSize(watchDir)
        .then((size) => {
          if (size !== lastSize) {
            lastSize = size;
            lastActivity = Date.now();
          }
        })
        .catch(() => undefined)
        .finally(() => {
          measuring = false;
        });
    }, Math.min(15_000, Math.max(250, Math.floor(stallMs / 4))));
    const onAbort = (): void => kill('cancelled');
    signal?.addEventListener('abort', onAbort, { once: true });

    const finish = (failure: RunFailure | null): void => {
      if (settled) return;
      settled = true;
      clearTimeout(overall);
      clearInterval(watchdog);
      signal?.removeEventListener('abort', onAbort);
      if (failure) reject(failure);
      else resolve();
    };

    child.on('error', (e) => finish(new RunFailure('spawn', `could not run ${argv[0]}: ${e.message}`, tail)));
    // close 而非 exit：exit 时 stdio 可能还有没读完的尾巴，close 保证流已读尽
    child.on('close', (code, sig) => {
      out.flush();
      err.flush();
      if (killedFor === 'cancelled') finish(new RunFailure('cancelled', 'cancelled', tail));
      else if (killedFor === 'timeout') {
        finish(new RunFailure('timeout', `timed out after ${Math.round(timeoutMs / 1000)}s`, tail));
      } else if (killedFor === 'stalled') {
        finish(new RunFailure('stalled', `no output and no disk activity for ${Math.round(stallMs / 1000)}s`, tail));
      } else if (code === 0) finish(null);
      else finish(new RunFailure('exit', `exited with code=${code}, signal=${sig}`, tail));
    });
  });
}

/** 数据目录所在盘的空闲字节数；查不到（老系统/网络盘）返回 null，调用方跳过检查。 */
export async function freeBytes(dir: string): Promise<number | null> {
  try {
    const st = await statfs(dir);
    const free = Number(st.bavail) * Number(st.bsize);
    return Number.isFinite(free) && free >= 0 ? free : null;
  } catch {
    return null;
  }
}

const gb = (bytes: number): string => `${(bytes / 1024 ** 3).toFixed(1)} GB`;

/** 改名重试几次：Windows 上刚退出的进程还会短暂占着文件。 */
async function renameWithRetry(from: string, to: string): Promise<void> {
  for (let i = 0; ; i++) {
    try {
      await rename(from, to);
      return;
    } catch (err) {
      if (i >= 4) throw err;
      await delay(500 * (i + 1));
    }
  }
}

/**
 * 引导（或复用）内嵌后端环境，返回 legacy-engine 的 command 配置。
 * 幂等：哨兵匹配时不产生任何子进程，只做几次文件读取。
 */
export async function bootstrapEngine(opts: BootstrapOptions): Promise<EngineCommand> {
  const { resourcesDir, dataDir, onProgress, signal } = opts;
  const baseEnv = opts.env ?? process.env;
  const timeouts: StepTimeouts = { ...DEFAULT_TIMEOUTS, ...opts.timeouts };
  const port = opts.port ?? ENGINE_PORT;

  const uvBin = join(resourcesDir, 'uv', process.platform === 'win32' ? 'uv.exe' : 'uv');
  const backendDir = join(resourcesDir, 'backend');
  for (const [what, path] of [
    ['uv 二进制', uvBin],
    ['后端源码', join(backendDir, 'pyproject.toml')],
    ['后端哈希', join(backendDir, '.hash')],
  ] as const) {
    if (!existsSync(path)) {
      throw new BootstrapError(
        'resources',
        `engine-bootstrap: 安装包里缺少${what}（${path}）——打包时没跑 stage:resources？`,
      );
    }
  }
  const uvVersion = readTrimmed(join(resourcesDir, 'uv', 'version.txt'));
  const backendHash = readTrimmed(join(backendDir, '.hash'));

  const engineDir = join(dataDir, 'engine');
  const venvDir = join(engineDir, 'venv');
  const nextDir = join(engineDir, 'venv.next');
  const prevDir = join(engineDir, 'venv.prev');
  const pythonIn = (dir: string): string =>
    process.platform === 'win32' ? join(dir, 'Scripts', 'python.exe') : join(dir, 'bin', 'python');
  const venvPython = pythonIn(venvDir);
  const sentinelPath = join(engineDir, 'bootstrap.json');
  const command = (): string[] => buildEngineCommand(venvPython, backendDir, engineDir, port);

  onProgress?.({ phase: 'check' });
  // 上次对调到一半就断了（venv 已挪走、新的还没就位）：把旧的放回原位
  if (!existsSync(venvDir) && existsSync(prevDir)) {
    await renameWithRetry(prevDir, venvDir).catch(() => undefined);
  }

  const wanted: Sentinel = { uvVersion, backendHash, pythonVersion: PYTHON_VERSION };
  let previous: Partial<Sentinel> | null = null;
  try {
    previous = JSON.parse(readFileSync(sentinelPath, 'utf8')) as Partial<Sentinel>;
  } catch {
    /* 哨兵不存在或损坏 → 全量引导 */
  }
  /** 有一套装完整的旧环境（哪怕是给上个版本装的）。 */
  const hadWorking = previous !== null && existsSync(venvPython);
  const fresh = !(
    hadWorking &&
    previous!.uvVersion === wanted.uvVersion &&
    previous!.backendHash === wanted.backendHash &&
    previous!.pythonVersion === wanted.pythonVersion
  );

  if (!fresh) {
    onProgress?.({ phase: 'ready' });
    return { mode: 'command', command: command(), port, steps: [] };
  }

  const note = (phase: BootstrapPhase, text: string): void => onProgress?.({ phase, line: `[polaris] ${text}` });
  const steps: StepReport[] = [];

  try {
    await mkdir(engineDir, { recursive: true });
    // 上次没装完留下的半成品不复用：装到一半的环境说不清缺什么
    await rm(nextDir, { recursive: true, force: true });
    await rm(prevDir, { recursive: true, force: true });

    const minFree = opts.minFreeBytes ?? MIN_FREE_BYTES;
    if (minFree > 0) {
      const free = await freeBytes(engineDir);
      if (free !== null && free < minFree) {
        throw new BootstrapError(
          'disk-space',
          `not enough free disk space: ${gb(free)} free on the disk holding ${engineDir}, ${gb(minFree)} needed`,
        );
      }
    }

    const mode = opts.network?.mirror ?? 'auto';
    const order = sourceOrder(mode, hasUserUvConfig(baseEnv), previous?.source);
    const rounds = Math.max(1, opts.rounds ?? 2);
    const retryDelayMs = opts.retryDelayMs ?? 2_000;
    /** 当前在用的源在 order 里的位置；某一步换源成功后，后面的步骤从它开始。 */
    let sticky = 0;

    /**
     * 跑一步。联网的步骤按源轮着试：当前源 → 其余的源，整轮都不成再来一轮。
     * 不联网的步骤（建 venv）换源没有意义，原地重试一次。
     */
    const step = async (
      phase: Exclude<BootstrapPhase, 'check' | 'ready'>,
      argv: string[],
      network: boolean,
      cleanup?: () => Promise<void>,
    ): Promise<void> => {
      onProgress?.({ phase });
      const started = Date.now();
      const plan: SourceId[] = [];
      if (network) {
        for (let r = 0; r < rounds; r++) {
          for (let i = 0; i < order.length; i++) plan.push(order[(sticky + i) % order.length]!);
        }
      } else {
        plan.push(order[sticky]!, order[sticky]!);
      }
      let last: RunFailure | null = null;
      for (let attempt = 0; attempt < plan.length; attempt++) {
        const source = plan[attempt]!;
        if (attempt > 0) {
          await cleanup?.();
          note(phase, `retrying with ${describeSource(source)} (attempt ${attempt + 1} of ${plan.length})`);
          try {
            await delay(retryDelayMs * Math.min(attempt, 5), undefined, { signal });
          } catch {
            throw new BootstrapError('cancelled', 'engine setup was cancelled');
          }
        } else if (network) {
          note(phase, `using ${describeSource(source)}`);
        }
        try {
          await run(argv, {
            env: buildUvEnv(baseEnv, engineDir, source, opts.network?.proxy),
            phase,
            onProgress,
            signal,
            timeoutMs: timeouts[phase],
            stallMs: timeouts.stall,
            watchDir: network ? engineDir : undefined,
          });
          sticky = order.indexOf(source);
          steps.push({ phase, source, attempts: attempt + 1, ms: Date.now() - started });
          return;
        } catch (err) {
          const failure = err instanceof RunFailure ? err : new RunFailure('exit', String(err), []);
          if (failure.reason === 'cancelled') throw new BootstrapError('cancelled', 'engine setup was cancelled');
          last = failure;
          note(phase, `failed with ${describeSource(source)}: ${failure.message}`);
          // uv 自己都跑不起来，换源也没用
          if (failure.reason === 'spawn') break;
        }
      }
      throw new BootstrapError(
        network ? 'download' : 'install',
        `engine-bootstrap: ${phase} step failed after ${plan.length} attempt(s): ${last?.message ?? 'unknown error'}\n${(last?.tail ?? []).join('\n')}`,
        last?.tail ?? [],
      );
    };

    await step('python', [uvBin, 'python', 'install', PYTHON_VERSION], true);

    // 全新建而不是在旧 venv 上增量升级：依赖集合可能变了，增量会攒出「卸载不净」
    // 的幽灵依赖。--relocatable：装好后要改名挪到 venv，脚本里不能写死路径。
    await step(
      'venv',
      [uvBin, 'venv', '--relocatable', '--python', PYTHON_VERSION, nextDir],
      false,
      () => rm(nextDir, { recursive: true, force: true }),
    );

    await step('install', [uvBin, 'pip', 'install', '--python', pythonIn(nextDir), backendDir], true);

    // 对调之前验一下新环境真能用：解释器起得来、关键依赖导得进
    await run([pythonIn(nextDir), '-X', 'utf8', '-c', 'import fastapi, uvicorn, alembic, sqlalchemy'], {
      env: buildUvEnv(baseEnv, engineDir, 'default'),
      phase: 'install',
      onProgress,
      signal,
      timeoutMs: 120_000,
      stallMs: 120_000,
    }).catch((err: unknown) => {
      const failure = err instanceof RunFailure ? err : new RunFailure('exit', String(err), []);
      if (failure.reason === 'cancelled') throw new BootstrapError('cancelled', 'engine setup was cancelled');
      throw new BootstrapError(
        'install',
        `engine-bootstrap: the new environment failed its import check: ${failure.message}\n${failure.tail.join('\n')}`,
        failure.tail,
      );
    });
    if (signal?.aborted) throw new BootstrapError('cancelled', 'engine setup was cancelled');

    // 对调：旧的先挪到 venv.prev，新的就位后才删。挪不动（文件被占）就把旧的放回去。
    if (existsSync(venvDir)) await renameWithRetry(venvDir, prevDir);
    try {
      await renameWithRetry(nextDir, venvDir);
    } catch (err) {
      if (existsSync(prevDir) && !existsSync(venvDir)) await renameWithRetry(prevDir, venvDir).catch(() => undefined);
      throw new BootstrapError('install', `engine-bootstrap: could not put the new environment in place: ${String(err)}`);
    }
    const sentinel: Sentinel = { ...wanted, source: order[sticky] };
    const tmp = `${sentinelPath}.tmp`;
    await writeFile(tmp, `${JSON.stringify(sentinel, null, 2)}\n`);
    await rename(tmp, sentinelPath);
    await rm(prevDir, { recursive: true, force: true }).catch(() => undefined);
  } catch (err) {
    await rm(nextDir, { recursive: true, force: true }).catch(() => undefined);
    const cancelled = err instanceof BootstrapError && err.code === 'cancelled';
    // 旧环境还在（升级途中失败）：这次继续用它。取消不算——用户正在退出。
    if (!cancelled && hadWorking && existsSync(venvPython)) {
      const reason = err instanceof Error ? err.message.split('\n', 1)[0]! : String(err);
      note('install', `could not prepare the updated environment (${reason}); keeping the previous one for this session`);
      onProgress?.({ phase: 'ready' });
      return { mode: 'command', command: command(), port, steps, degraded: { reason } };
    }
    throw err;
  }

  onProgress?.({ phase: 'ready' });
  return { mode: 'command', command: command(), port, steps };
}

/**
 * 引擎启动 argv：与 legacy-engine docker 模式的 `sh -lc "alembic && uvicorn"`
 * 同构，但 command 模式没有 shell，所以用 python -c 的小启动器串联两步。
 * 启动器同时负责 cwd、数据库地址与用户数据目录：
 * - chdir 到后端源码目录——alembic.ini 的 script_location/prepend_sys_path
 *   都是相对 cwd 的相对路径；
 * - POLARIS_DATABASE_URL 指到 userData 的 SQLite 文件（数据库路径是引导方
 *   才知道的信息，所以写在启动器里）；
 * - POLARIS_DATA_DIR 指到 userData 的 engine/data/（#718）：后端 data_dir
 *   默认相对 './data'，chdir 之后会落进 App 安装包的 resources/backend/，
 *   应用更新整包替换时用户的 PDF/导出/实验日志就全没了。setdefault 而非
 *   覆写：给高级用户留 env 改道的口子。
 */
function buildEngineCommand(venvPython: string, backendDir: string, engineDir: string, port: number): string[] {
  const dbPath = join(engineDir, 'polaris.db').split('\\').join('/');
  const snapshotRoot = join(engineDir, 'snapshots').split('\\').join('/');
  const dataDir = join(engineDir, 'data').split('\\').join('/');
  const legacyDataDir = join(backendDir, 'data').split('\\').join('/');
  // JSON.stringify 产出的字符串字面量对 Python 同样合法（转义子集兼容），
  // 借它安全嵌入含空格/反斜杠/非 ASCII 的路径
  const launcher = [
    'import os, subprocess, sys',
    // 传给 alembic 子进程（子解释器启动时读 env；本进程靠 -X utf8）
    "os.environ.setdefault('PYTHONUTF8', '1')",
    `os.environ['POLARIS_DATABASE_URL'] = ${JSON.stringify(`sqlite+aiosqlite:///${dbPath}`)}`,
    // 用户数据目录钉在 userData 下（#718，理由见本函数 docstring）
    `os.environ.setdefault('POLARIS_DATA_DIR', ${JSON.stringify(dataDir)})`,
    `os.chdir(${JSON.stringify(backendDir)})`,
    ...buildDataDirMigration(legacyDataDir),
    ...buildMigrationGuard(dbPath, snapshotRoot, "[sys.executable, '-m', 'alembic', 'upgrade', 'head']"),
    'import uvicorn',
    `uvicorn.run('app.main:app', host='127.0.0.1', port=${port})`,
  ].join('\n');
  // -X utf8：启动器自身的解释器也走 UTF-8 模式（env 对已启动的进程无效）
  return [venvPython, '-X', 'utf8', '-c', launcher];
}

/**
 * 旧数据目录一次性搬迁的 Python 片段（#718）：修复前的桌面版把用户文件
 * 写进了 <resources/backend>/data（chdir 后的相对 './data'）——那里随应用
 * 更新整包替换。首个修复版启动时：旧位置非空且新位置（POLARIS_DATA_DIR，
 * 读 env 以尊重用户覆写）还没有内容 → shutil.move 整体搬过去。
 *
 * 只搬一次：搬成功后旧位置消失，之后每次启动的判断都是空操作；新位置
 * 已有内容时绝不合并（说明用户已在新位置积累了数据，盲目合并可能覆盖）。
 * 失败只打日志不阻断启动——宁可这一轮继续用旧位置的文件（backend 兜底
 * 已把相对 data_dir resolve 到 cwd，行为与修复前一致），也不能让引擎
 * 起不来；且任何路径下都不主动删除源目录（shutil.move 失败时源保持原样）。
 *
 * 放在启动器里而不是 TS 侧：与 alembic 同理（见下），bootstrapEngine 有
 * 哨兵会整段跳过，而搬迁必须每次 spawn 都检查；纯 stdlib，不加依赖。
 * 片段自带 import，可独立喂给 python -c（bootstrap-smoke 单独驱动验证）。
 */
export function buildDataDirMigration(legacyDataDir: string): string[] {
  return [
    'import os, shutil',
    `_old_data = ${JSON.stringify(legacyDataDir)}`,
    "_new_data = os.environ['POLARIS_DATA_DIR']",
    'try:',
    '    if os.path.isdir(_old_data) and os.listdir(_old_data):',
    '        if not os.path.isdir(_new_data) or not os.listdir(_new_data):',
    // shutil.move 遇到已存在的目标目录会把源搬进目标里面（变成 data/data）；
    // 空目录先 rmdir 掉，让 move 落在正确的一层。rmdir 只删空目录，安全。
    '            if os.path.isdir(_new_data):',
    '                os.rmdir(_new_data)',
    "            print(f'engine-launcher: 迁移旧数据目录 {_old_data} -> {_new_data}', flush=True)",
    '            shutil.move(_old_data, _new_data)',
    '    os.makedirs(_new_data, exist_ok=True)',
    'except Exception as _e:',
    "    print(f'engine-launcher: 旧数据目录迁移失败（{_e}），保留 {_old_data}，继续启动', flush=True)",
  ];
}

/**
 * 迁移守卫的 Python 片段（#694）：跑迁移命令前把非空库快照到
 * snapshots/<时间戳>/，命令失败（check=True 抛 CalledProcessError）先把
 * 快照复制回原位再重新抛出——引擎照现有流程失败退出/回落，但库保证
 * 停在迁移前；成功则只保留最近 3 份快照。
 *
 * 为什么放在启动器 Python 串里而不是 TS 侧：bootstrapEngine 有哨兵，
 * 后端没变化的启动整段跳过，而 alembic 是引擎每次 spawn 都会跑的——
 * 快照必须与 alembic 同进程同时机，TS 侧根本不知道它何时执行。
 * 纯 stdlib（os/shutil/datetime/subprocess），不给后端加任何依赖。
 *
 * `migrateArgv` 是一个 Python 表达式字符串（如
 * "[sys.executable, '-m', 'alembic', 'upgrade', 'head']"），原样嵌进
 * subprocess.run(...)；表达式形态让 bootstrap-smoke 能塞入伪造的失败
 * 命令单独验证「还原快照」路径。片段自带 import，可独立喂给 python -c。
 */
export function buildMigrationGuard(dbPath: string, snapshotRoot: string, migrateArgv: string): string[] {
  return [
    'import datetime, os, shutil, subprocess, sys',
    `_db = ${JSON.stringify(dbPath)}`,
    `_snap_root = ${JSON.stringify(snapshotRoot)}`,
    // -wal 里可能有未合并的写入，只拷主文件会丢数据；三个文件成组进退
    "_sfx = ('', '-wal', '-shm')",
    '_snap = None',
    // 空库/不存在（首启）不快照：没有可保护的数据就不留空目录
    'if os.path.exists(_db) and os.path.getsize(_db) > 0:',
    // 冒号在 Windows 文件名里非法；%f（微秒）让同秒重启也不撞名
    "    _stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H-%M-%S.%fZ')",
    '    _snap = os.path.join(_snap_root, _stamp)',
    '    os.makedirs(_snap, exist_ok=True)',
    '    for _s in _sfx:',
    '        if os.path.exists(_db + _s):',
    '            shutil.copy2(_db + _s, os.path.join(_snap, os.path.basename(_db) + _s))',
    'try:',
    `    subprocess.run(${migrateArgv}, check=True)`,
    'except BaseException:',
    '    if _snap is not None:',
    '        for _s in _sfx:',
    '            _saved = os.path.join(_snap, os.path.basename(_db) + _s)',
    '            if os.path.exists(_saved):',
    '                shutil.copy2(_saved, _db + _s)',
    // 快照里没有的伴生文件要删掉现场的：失败迁移留下的新 -wal
    // 会在还原后的主文件上重放，等于又把库弄脏
    '            elif os.path.exists(_db + _s):',
    '                os.remove(_db + _s)',
    '    raise',
    'if _snap is not None:',
    '    _dirs = sorted(_d for _d in os.listdir(_snap_root) if os.path.isdir(os.path.join(_snap_root, _d)))',
    '    for _old in _dirs[:-3]:',
    '        shutil.rmtree(os.path.join(_snap_root, _old), ignore_errors=True)',
  ];
}
