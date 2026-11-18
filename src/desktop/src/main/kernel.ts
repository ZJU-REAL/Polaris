/* ============================================================
   @polaris/kernel 在桌面主进程里的挂载点。

   组织方式：模块级单例 + 具名访问函数，
   不把 kernel 实例挂到全局或到处传。生命周期绑定 app：
   ready 时 start，退出路径 stop（fiber.dispose 级联回收所有插件注册
   的副作用——计时器、监听器、子进程）。

   装载结构：
   - storage（#608/#609）：SQLite 持久层，直挂的地基插件——配置树本身就
     存在它的 SqliteConfigTreeStore 里。插件用的是 Electron 内嵌 Node 的
     node:sqlite（要求 Node ≥ 22），这正是 Electron 必须升到 44 的原因。
   - Loader + SqliteTree（#699/#703）：用户装的插件由持久化配置树驱动。
     树里不再有任何内置条目；旧版本种下的三条（desktop-probe / sources /
     legacy-engine）由 kernel 在装载前清掉，树装不起来时备份后重置
     （见 @polaris/kernel 的 host.ts）。
   - 本机引擎：同 storage 一样直挂（mountEngine），不进配置树。引擎参数
     每次启动现算——以前经树条目注入，任何一次树写入都会把带绝对路径的
     启动命令原样落库，下次启动树先拿着旧路径把引擎拉起来。配置来源：
       1. POLARIS_DESKTOP_ENGINE 显式指定（开发/调试）
       2. 打包态自动引导（#607）：安装包自带 uv + 后端源码，首启在 userData
          里装出 venv 后以 command 模式拉起——用户机器不需要 Python/docker
     两者都不成立（开发态未设 env）时没有引擎，前端显示「本机引擎没有运行」页。

   引擎起来之后由插件自己看着（崩溃/卡死自动重启一次）；状态变化经
   onEngineState 传出去，index.ts 再转给界面。

   退出可以随时打断启动：引导中的 uv 子进程、等引擎健康的轮询都会立刻停下，
   不用等它们跑完（见 stopKernel）。
   ============================================================ */

import { app } from 'electron';

import { join } from 'node:path';

import {
  createPluginHost,
  enginePortProblem,
  ensureShellEnv,
  marketPluginsDir as kernelMarketPluginsDir,
  mountEngine,
  type EnginePortProblem,
  type Kernel,
  type LegacyEngineConfig,
  type LegacyEngineService,
  type PluginMetaStore,
  type SqliteTree,
  type StorageService,
} from '@polaris/kernel';

import type {
  EngineBootstrapStatus,
  EngineFailure,
  EngineRunState,
  KernelStatus,
  LocalBackendInfo,
} from '../shared/contract';
import { BootstrapProgressTracker } from './bootstrap-progress';
import { BootstrapError, bootstrapEngine } from './engine-bootstrap';
import { engineLogPath, logTail, scrub, writeBootstrapLog } from './logging';
import { configuredProxy, resolveBootstrapNetwork } from './network';

let kernel: Kernel | null = null;

/** 已经建出来、但启动流程还没走完的实例。退出时要能直接停它，不必等启动收尾。 */
let pendingKernel: Kernel | null = null;

/** 退出进行中：启动流程每到一个检查点看它一眼，是就不再往下走。 */
let stopping = false;

/** 正在跑的首启引导的取消口。 */
let bootstrapAbort: AbortController | null = null;

/**
 * 市场安装物的落盘根（#708）：userData/plugins/<包名>/<版本>/。刻意在
 * asar 之外——打包态 asar 内容不可写，且 loader 对三方包走真实的动态
 * import，必须是文件系统上的普通路径。
 */
export function marketPluginsDir(): string {
  return kernelMarketPluginsDir(app.getPath('userData'));
}

/**
 * 内嵌引擎引导进度，kernel.engineBootstrapStatus 直接读它（#721 起窗口先于
 * 内核创建，渲染层的首启等待页轮询这个状态）。
 * - starting：内核启动中，还不知道走不走内嵌路径（初始态）
 * - check/python/venv/install：内嵌引导各阶段（见 engine-bootstrap.ts）
 * - engine：环境已装好，引擎进程启动中（首启迁移可能要一会儿）；
 *   引擎中途退出、正在自动重启时也回到这个阶段
 * - ready + done：引擎已健康，本地地址可用
 * - idle + done：没走内嵌路径（开发态 / 显式 env / 无引擎）
 * - failed + done：内嵌引导或引擎启动失败，或运行中退出且没救回来
 */
let bootstrapStatus: EngineBootstrapStatus = { phase: 'starting', done: false };

/**
 * 引导进度的细节（阶段耗时、输出尾巴、落盘字节数），只在走内嵌引导时存在。
 * 读状态时现算合并——字节数在两行输出之间也在涨，不能只在回调里快照。
 */
let bootstrapTracker: BootstrapProgressTracker | null = null;

/** 设置引导阶段，同步给 tracker 切阶段；done 时停掉量盘。 */
function setBootstrapStatus(phase: string, done: boolean): void {
  bootstrapStatus = { phase, done };
  if (!bootstrapTracker) return;
  bootstrapTracker.phase(phase);
  if (done) bootstrapTracker.stop();
}

/** 本次启动引擎失败的已知原因（#850：端口被别的程序/残留旧引擎占着），给兜底页说清楚。 */
let engineProblem: EnginePortProblem | null = null;

/** 引擎没准备好/没起来/没救回来的原因与最后几行输出，给兜底页与诊断信息。 */
let engineFailure: EngineFailure | null = null;

/** 新环境没装成、这次沿用旧环境时的原因（见 engine-bootstrap 的 degraded）。 */
let engineDegraded: string | null = null;

/** 引擎运行中退出、正在自动重启。 */
let engineRestarting = false;

export type EngineStateListener = (event: { state: 'running' | 'restarting' | 'failed'; reason?: string }) => void;
const engineStateListeners = new Set<EngineStateListener>();

/** 订阅引擎运行状态变化（崩溃后重启中 / 回来了 / 没救回来）；返回取消订阅函数。 */
export function onEngineState(listener: EngineStateListener): () => void {
  engineStateListeners.add(listener);
  return () => engineStateListeners.delete(listener);
}

function failure(code: EngineFailure['code'], message: string, lines: string[]): EngineFailure {
  return {
    code,
    message: scrub(message),
    // 界面上放不下太多，也没必要：完整的在日志文件里
    lines: lines.map((line) => scrub(line)).filter((line) => line.trim()).slice(-12),
    at: Date.now(),
  };
}

/**
 * POLARIS_DESKTOP_ENGINE 的取值：
 *   docker:<image>:<backendDirAbs>   如 docker:polaris-api-test:local:/repo/src/backend
 *   command:<json argv>              如 command:["node","engine.js"]
 * 未设置 / 解析失败 = 不用显式配置（打包态转入自动引导，开发态不装引擎）。
 */
function parseEngineSpec(raw: string | undefined): LegacyEngineConfig | null {
  if (!raw) return null;
  if (raw.startsWith('command:')) {
    try {
      const argv: unknown = JSON.parse(raw.slice('command:'.length));
      if (Array.isArray(argv) && argv.length > 0 && argv.every((a) => typeof a === 'string')) {
        return { mode: 'command', command: argv as string[] };
      }
    } catch {
      /* 落到末尾的统一告警 */
    }
  } else if (raw.startsWith('docker:')) {
    const rest = raw.slice('docker:'.length);
    // 镜像名可带 tag（含冒号），backendDir 是绝对路径：从右往左找第一个
    // 「其后是绝对路径」的冒号做分隔，避免把 tag 里的冒号切错。
    for (let i = rest.length - 1; i > 0; i--) {
      if (rest[i] !== ':') continue;
      const dir = rest.slice(i + 1);
      if (dir.startsWith('/') || /^[A-Za-z]:[\\/]/.test(dir)) {
        return { mode: 'docker', image: rest.slice(0, i), backendDir: dir };
      }
    }
  }
  console.error(`[kernel] 无法解析 POLARIS_DESKTOP_ENGINE：${raw}`);
  return null;
}

/**
 * 打包态的自动引导：用安装包里的 uv + 后端源码在 userData 装出运行环境，
 * 返回 legacy-engine 的 command 配置。失败返回 null（前端显示失败页），
 * 绝不让引导问题挡住窗口创建。
 *
 * resourcesDir 平时就是安装包的资源目录；开发时可用
 * POLARIS_DESKTOP_ENGINE=bootstrap:<目录> 指到 `pnpm run stage:resources` 的产物
 * （src/desktop/resources），不打包也能把首启安装这条路从头走一遍。
 */
async function bootstrapPackagedEngine(resourcesDir: string, port?: number): Promise<LegacyEngineConfig | null> {
  const dataDir = app.getPath('userData');
  bootstrapTracker = new BootstrapProgressTracker({ engineDir: join(dataDir, 'engine') });
  bootstrapAbort = new AbortController();
  try {
    const network = await resolveBootstrapNetwork();
    // 只记来源种类，不记地址（可能带账号密码）
    writeBootstrapLog(`[polaris] bootstrap starting: proxy=${network.proxySource}, mirror=${network.mirror ?? 'auto'}`);
    const result = await bootstrapEngine({
      resourcesDir,
      dataDir,
      port,
      signal: bootstrapAbort.signal,
      network,
      onProgress: ({ phase, line }) => {
        setBootstrapStatus(phase, false);
        // 首启会下载 Python 工具链：全部输出进 bootstrap.log，首启等待页显示尾巴
        if (line) {
          writeBootstrapLog(line);
          bootstrapTracker?.line(line);
        }
      },
    });
    for (const step of result.steps) {
      writeBootstrapLog(
        `[polaris] ${step.phase}: ok via ${step.source} after ${step.attempts} attempt(s), ${(step.ms / 1000).toFixed(1)}s`,
      );
    }
    if (result.degraded) {
      engineDegraded = scrub(result.degraded.reason);
      console.warn(`[kernel] 新环境没装成，本次沿用上一套环境：${engineDegraded}`);
    }
    // 环境装好 ≠ 可用：引擎进程还要跑迁移并通过健康检查（下方 mountEngine
    // 才等它），done 必须等引擎真的健康——否则首启等待页会提前放行，
    // 前端探测拿到 null 就落到「引擎没启动」页，等待整段白等。
    setBootstrapStatus('engine', false);
    return { mode: result.mode, command: result.command, port: result.port };
  } catch (err) {
    if (err instanceof BootstrapError && err.code === 'cancelled') {
      writeBootstrapLog('[polaris] bootstrap cancelled (app is quitting)');
      console.log('[kernel] 首启引导已取消（正在退出）');
      return null;
    }
    const message = err instanceof Error ? err.message : String(err);
    const code = err instanceof BootstrapError && err.code !== 'cancelled' ? err.code : 'install';
    const tail = err instanceof BootstrapError && err.tail.length ? err.tail : message.split('\n').slice(1);
    engineFailure = failure(code, message.split('\n', 1)[0] ?? message, tail);
    writeBootstrapLog(`[polaris] bootstrap failed (${code}): ${message}`);
    setBootstrapStatus('failed', true);
    console.error('[kernel] 内嵌引擎引导失败：', message.split('\n', 1)[0]);
    return null;
  } finally {
    bootstrapAbort = null;
  }
}

/** 引擎健康之后：把它之后的状态变化（中途退出、自动重启、没救回来）接出来。 */
function watchEngine(instance: Kernel): void {
  const legacy = instance.ctx.get('legacy') as LegacyEngineService | undefined;
  if (!legacy) return;
  legacy.onState((event) => {
    if (stopping) return;
    if (event.state === 'restarting') {
      engineRestarting = true;
      setBootstrapStatus('engine', false);
      console.warn(`[kernel] 本机引擎中途退出，正在自动重启：${event.reason ?? ''}`);
    } else if (event.state === 'running') {
      engineRestarting = false;
      setBootstrapStatus('ready', true);
      console.log('[kernel] 本机引擎已恢复');
    } else {
      engineRestarting = false;
      engineFailure = failure('engine-crashed', event.reason ?? 'the local engine stopped', logTail(engineLogPath(), 12));
      setBootstrapStatus('failed', true);
      console.error(`[kernel] 本机引擎没能恢复：${event.reason ?? ''}`);
    }
    for (const listener of [...engineStateListeners]) {
      try {
        listener({ state: event.state, reason: event.reason ? scrub(event.reason.split('\n', 1)[0]!) : undefined });
      } catch (err) {
        console.error('[kernel] engine state listener failed:', err);
      }
    }
  });
}

let startingKernel: Promise<Kernel> | null = null;

/** 幂等启动：重复调用返回同一实例（app ready 与 smoke 都可能触发）。
    #721 起启动在窗口之后台进行，启动中的并发调用共享同一个 Promise。 */
export async function startKernel(): Promise<Kernel> {
  if (kernel) return kernel;
  startingKernel ??= doStartKernel().finally(() => {
    startingKernel = null;
  });
  return startingKernel;
}

async function doStartKernel(): Promise<Kernel> {
  // smoke 等场景会 stop 后再次 start：进度回到初始态，别让上一轮的
  // ready/failed 冒充本轮结论
  bootstrapTracker?.stop();
  bootstrapTracker = null;
  bootstrapStatus = { phase: 'starting', done: false };
  engineProblem = null;
  engineFailure = null;
  engineDegraded = null;
  engineRestarting = false;
  // 从访达/程序坞启动的应用拿不到 ~/.zshrc 里 export 的变量：引擎读的 Claude API
  // 密钥与地址、uv 要用的代理与镜像设置都会落空。这里向登录 shell 要一次（只取
  // 那几个名字、已有的不覆盖、Windows 不做）。现在就发起、与下面的装配并行，
  // 用到之前才等它：shell 再慢也有超时兜底，每次应用启动只跑一次。
  const shellEnv = ensureShellEnv();
  // 装配顺序（storage → baseUrl → 哈希复核 → 清理旧条目 → Loader → SqliteTree）
  // 住在 @polaris/kernel 的 createPluginHost 里（#754）。
  const { kernel: instance, repairs } = await createPluginHost({
    name: 'polaris-desktop',
    dataRoot: app.getPath('userData'),
    // 树里只有用户装的插件；全新安装就是一棵空树
    seedEntries: [],
    strict: app.isPackaged,
  });
  pendingKernel = instance;
  try {
    for (const repair of repairs) {
      if (repair.kind === 'reset') {
        console.warn(`[kernel] 插件配置读不出来，已重置；原内容备份在 ${repair.backupFile ?? repair.backupKey ?? '（备份失败）'}`);
      }
    }

    const spec = process.env.POLARIS_DESKTOP_ENGINE;
    // bootstrap:<资源目录>：开发时把首启安装这条路走一遍（见 bootstrapPackagedEngine）
    const devBootstrapDir = spec?.startsWith('bootstrap:') ? spec.slice('bootstrap:'.length) : null;
    const portOverride = Number(process.env.POLARIS_DESKTOP_ENGINE_PORT);
    const port = Number.isInteger(portOverride) && portOverride > 0 ? portOverride : undefined;
    let engine = stopping || devBootstrapDir ? null : parseEngineSpec(spec);
    // 并行隔离（壳级 E2E / 同机多实例）：docker 容器名与宿主端口默认是固定值，
    // 两个实例同时跑必然撞名撞端口。这两个 env 只在显式指定引擎的测试/调试
    // 场景使用，安装包自己的引导不经过它们。
    if (engine) {
      const name = process.env.POLARIS_DESKTOP_ENGINE_CONTAINER;
      if (name) engine = { ...engine, containerName: name };
      if (port) engine = { ...engine, port };
    }
    const resourcesDir = devBootstrapDir ?? (app.isPackaged ? process.resourcesPath : null);
    if (!engine && resourcesDir && !stopping) {
      // uv 的环境在 spawn 那一刻从 process.env 取：登录 shell 里的代理/镜像设置
      // 必须先落定。只记名字，值绝不进日志。
      const picked = await shellEnv;
      if (picked.length) console.log(`[kernel] 从登录 shell 取得环境变量：${picked.join(', ')}`);
      engine = await bootstrapPackagedEngine(resourcesDir, devBootstrapDir ? port : undefined);
    }
    if (engine && !stopping) {
      // 引擎安全（#850）：每份安装的密钥文件与孤儿引擎记录都放在 userData。
      // 只传路径——密钥本身由 legacy-engine 插件读出后经环境变量交给引擎。
      const userData = app.getPath('userData');
      engine = {
        ...engine,
        secretsFile: join(userData, 'engine-secrets.json'),
        pidFile: join(userData, 'engine.pid.json'),
        logFile: engineLogPath(),
      };
      // 引擎子进程的环境同样在 spawn 那一刻取，必须先等 shell 那边落定。
      const picked = await shellEnv;
      if (picked.length && !resourcesDir) {
        console.log(`[kernel] 从登录 shell 取得环境变量：${picked.join(', ')}`);
      }
      // 设置里填了代理：引擎查文献（arXiv / Semantic Scholar / OpenAlex）也走它。
      // 用户自己设了 POLARIS_OUTBOUND_PROXY 就听用户的；后端只认 http(s) 代理。
      const proxy = configuredProxy();
      if (proxy && /^https?:\/\//i.test(proxy) && !process.env.POLARIS_OUTBOUND_PROXY) {
        process.env.POLARIS_OUTBOUND_PROXY = proxy;
      }
      try {
        // resolve 时引擎已健康；抛错时子进程已被插件自己回收。#721 起窗口先于
        // 内核创建：首个文档的 CSP 可能没放行本地引擎，前端首启等待页在
        // bootstrapStatus done 后 reload 拿到最终 CSP 与地址。
        await mountEngine(instance, engine);
        watchEngine(instance);
      } catch (err) {
        if (!stopping) {
          engineProblem = enginePortProblem(err);
          const message = err instanceof Error ? err.message : String(err);
          engineFailure = failure('engine-start', message.split('\n', 1)[0] ?? message, message.split('\n').slice(1));
          console.error('[kernel] 本地引擎启动失败：', err);
        }
      }
    }

    if (stopping) throw new Error('kernel start cancelled: the app is quitting');
    await instance.start();
  } catch (err) {
    pendingKernel = null;
    // 没走完的启动不留半个内核在后台（引擎子进程、打开着的库文件）
    await instance.stop().catch(() => undefined);
    throw err;
  }
  kernel = instance;
  pendingKernel = null;
  // 引导进度收尾：走了内嵌路径的（engine 阶段）以引擎服务是否真挂出为准；
  // 没走内嵌路径的（starting 一路没变过：开发态 / 显式 env / 无引擎）标成
  // idle——env 引擎的健康等待已在上方 mountEngine 完成，前端拿 done 后
  // 探测 localBackend 即得最终答案。failed（引导抛错）保持原样不覆盖。
  if (bootstrapStatus.phase === 'engine') {
    setBootstrapStatus(localBackend().baseUrl ? 'ready' : 'failed', true);
  } else if (bootstrapStatus.phase === 'starting') {
    setBootstrapStatus('idle', true);
  }
  return instance;
}

/**
 * 停机：dispose 根 fiber，级联回收（含本地引擎子进程）。幂等，未启动时是
 * no-op。落盘顺序由 disposer 的逆序执行保证：SqliteTree 比 storage 后装，
 * 先被 dispose——其 stop() 里 root.stop() 之后有最后一次 flush——storage
 * 关库排在其后，不需要在这里显式 flush。
 *
 * 启动进行中也能停，而且不等它跑完：以前这里是「等启动收尾再停」，首启要下载
 * 几百兆，用户在这期间按退出就什么都不发生。现在先打断——取消引导（uv 连同
 * 子进程一起收掉）、停掉已经建出来的实例（等引擎健康的轮询随即放弃、引擎
 * 子进程被回收）——启动流程在下一个检查点自己收尾。
 */
export async function stopKernel(): Promise<void> {
  stopping = true;
  try {
    bootstrapAbort?.abort();
    const early = pendingKernel;
    if (early) await early.stop().catch((err) => console.error('[kernel] stop (mid-start) failed:', err));
    if (startingKernel) await startingKernel.catch(() => undefined);
    const instance = kernel;
    kernel = null;
    if (instance) await instance.stop();
  } finally {
    stopping = false;
  }
}

/**
 * kernel.status 的实现。plugins 用 ctx.registry.size —— cordis 公开文档口径
 * 「已注册插件 runtime 的数量」（vendor/deepseek-cordis/cordis/src/registry.ts），
 * 不数 fiber：同一插件多次装载仍算一个 runtime。名额包括直挂的 storage、
 * Loader（连带其内部 isolate）、SqliteTree、起来了的本机引擎，以及用户装的插件。
 */
export function kernelStatus(): KernelStatus {
  return {
    started: kernel?.started ?? false,
    name: kernel?.name ?? '',
    plugins: kernel ? kernel.ctx.registry.size : 0,
    // reflect 读服务：storage 插件 fiber 处于 ACTIVE 时才非空，
    // 「装了但没起来」与「没装」在这里同样折叠成 false。
    storage: (kernel?.ctx.get('storage') as StorageService | undefined) != null,
  };
}

/** kernel.engineBootstrapStatus 的实现：内嵌引擎引导进度（诊断/进度条用）。 */
export function engineBootstrapStatus(): EngineBootstrapStatus {
  const status: EngineBootstrapStatus = bootstrapTracker
    ? { ...bootstrapStatus, ...bootstrapTracker.snapshot() }
    : { ...bootstrapStatus };
  if (engineRestarting) status.restarting = true;
  if (status.phase === 'failed' && engineFailure) status.failure = engineFailure;
  return status;
}

/** 诊断信息用：引擎失败原因、降级说明。 */
export function engineFailureInfo(): { failure: EngineFailure | null; degraded: string | null } {
  return { failure: engineFailure, degraded: engineDegraded };
}

/**
 * plugins.* IPC 与 plugins.manage 能力位共用的树句柄。reflect 严格模式
 * 保证：kernel 没起、SqliteTree 没装、或其 fiber 不在 ACTIVE，这里一律
 * 拿到 null——能力位与方法族的「可用」判断因此天然是同一个事实。
 */
export function kernelConfigTree(): SqliteTree | null {
  return (kernel?.ctx.get('configTree') as SqliteTree | undefined) ?? null;
}

/**
 * importTree 存 last-good 快照的落点。storage 挂载失败（内存树会话）时为
 * null，导入照常进行只是少一层持久保险——树本身这次会话也不落盘。
 */
export function kernelPluginMeta(): PluginMetaStore | null {
  return (kernel?.ctx.get('storage') as StorageService | undefined)?.pluginMeta ?? null;
}

function legacyService(): LegacyEngineService | undefined {
  return kernel?.ctx.get('legacy') as LegacyEngineService | undefined;
}

/** 引擎进程的运行状态；没有引擎（没装、没起来）为 none。 */
export function engineRunState(): EngineRunState {
  const legacy = legacyService();
  if (!legacy) return 'none';
  // 老形状的服务没有 state()（测试替身）：有服务就算在跑
  return typeof legacy.state === 'function' ? legacy.state() : 'running';
}

/** 引擎内存里的最近输出（没配日志文件时的兜底）。 */
export function engineLogTailInMemory(): string[] {
  const legacy = legacyService();
  return legacy ? legacy.logTail().split('\n').filter(Boolean) : [];
}

/**
 * kernel.localBackend 的实现。ctx.get 是 cordis 公开的免 inject 读服务入口
 * （reflect mixin，见 vendor/deepseek-cordis/cordis/src/reflect.ts），严格模式
 * 只在提供方 fiber 处于 ACTIVE 时返回值——引擎没装、还没健康、或已失败时
 * 一律拿到 undefined，统一折叠成 null。引擎中途退出（正在重启或没救回来）时
 * 服务还在，但地址此刻不可用，同样交 null。
 */
export function localBackend(): LocalBackendInfo {
  const legacy = legacyService();
  const state = engineRunState();
  const usable = legacy != null && state === 'running';
  return {
    baseUrl: usable ? legacy.baseUrl : null,
    // 本次启动的本地会话口令（#850）：只经这条 IPC 交给自己的界面
    sessionSecret: usable ? legacy.sessionSecret : null,
    problem: usable ? null : engineProblem,
    state,
  };
}
