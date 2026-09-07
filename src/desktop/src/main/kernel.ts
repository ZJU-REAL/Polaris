/* ============================================================
   @polaris/kernel 在桌面主进程里的挂载点。

   与 agent/supervisor.ts 同样的组织方式：模块级单例 + 具名访问函数，
   不把 kernel 实例挂到全局或到处传。生命周期绑定 app：
   ready 时 start，退出路径 stop（fiber.dispose 级联回收所有插件注册
   的副作用——计时器、监听器、子进程）。

   装载结构（#703 起树驱动）：
   - storage（#608/#609）：SQLite 持久层，唯一直挂的地基插件——loader
     的配置树本身就存在它的 SqliteConfigTreeStore 里，进树会自举成环
     （见 kernel 的 builtins.ts 文件头）。插件用的是 Electron 内嵌 Node 的
     node:sqlite（要求 Node ≥ 22），这正是 Electron 必须升到 44 的原因——
     33 内嵌 Node 20，装载即 ERR_UNKNOWN_BUILTIN_MODULE。
   - Loader + SqliteTree（#699/#703）：其余插件一律由持久化配置树驱动，
     条目名走 cordis:<键> 查 loader.builtins 表，打包态零动态 import。
     首启种下 desktop-probe / sources / legacy-engine 三条目；此后树内容
     以用户状态为真相，启动时不再增删改写。
   - legacy-engine：树上只是 disabled 占位条目，引擎参数每次启动现算后
     动态注入（见 startKernel 内注释）。配置来源按优先级：
       1. POLARIS_DESKTOP_ENGINE 显式指定（开发/调试，行为与从前完全一致）
       2. 打包态自动引导（#607）：安装包自带 uv + 后端源码，首启在 userData
          里装出 venv 后以 command 模式拉起——用户机器不需要 Python/docker
     两者都不成立（开发态未设 env）时不注入，条目保持 disabled，走远端流程。
   ============================================================ */

import { app } from 'electron';
import { join } from 'node:path';

import {
  Loader,
  MemoryConfigTreeStore,
  SqliteTree,
  createKernel,
  registerBuiltins,
  storage,
  type ConfigEntry,
  type ConfigTreeStore,
  type Kernel,
  type LegacyEngineConfig,
  type PluginMetaStore,
  type StorageService,
} from '@polaris/kernel';

import type { EngineBootstrapStatus, KernelStatus, LocalBackendInfo } from '../shared/contract';
import { bootstrapEngine } from './engine-bootstrap';

let kernel: Kernel | null = null;

/**
 * 内嵌引擎引导进度，kernel.engineBootstrapStatus 直接读它。
 * idle = 没走内嵌路径（开发态 / 显式 env）；failed = 引导失败已回落远端。
 * 渲染层的进度条二期再接，这里先把契约与状态源立起来。
 */
let bootstrapStatus: EngineBootstrapStatus = { phase: 'idle', done: false };

/**
 * 首启种子树。只在 store 完全为空时写入：树是用户状态的真相，任何非空
 * 内容都不做「补齐」——那会把用户显式删掉的条目悄悄种回来。
 * legacy-engine 种成 disabled 占位：引擎能不能起、以什么参数起，每次
 * 启动才知道（env / 打包引导），树里只留位置不留答案。
 */
const SEED_ENTRIES: ConfigEntry[] = [
  { id: 'desktop-probe', name: 'cordis:desktop-probe' },
  { id: 'sources', name: 'cordis:sources' },
  { id: 'legacy-engine', name: 'cordis:legacy-engine', disabled: true },
];

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
 * 返回 legacy-engine 的 command 配置。失败返回 null（回落远端流程），
 * 绝不让引导问题挡住窗口创建。
 */
async function bootstrapPackagedEngine(): Promise<LegacyEngineConfig | null> {
  try {
    const config = await bootstrapEngine({
      resourcesDir: process.resourcesPath,
      dataDir: app.getPath('userData'),
      onProgress: ({ phase, line }) => {
        bootstrapStatus = { phase, done: false };
        // 首启会下载 Python 工具链，日志是唯一的可观测面（进度条二期接）
        if (line) console.log(`[engine-bootstrap] ${line}`);
      },
    });
    bootstrapStatus = { phase: 'ready', done: true };
    return config;
  } catch (err) {
    bootstrapStatus = { phase: 'failed', done: true };
    console.error('[kernel] 内嵌引擎引导失败，回落远端流程：', err);
    return null;
  }
}

/** 幂等启动：重复调用返回同一实例（app ready 与 smoke 都可能触发）。 */
export async function startKernel(): Promise<Kernel> {
  if (kernel) return kernel;
  const instance = createKernel({ name: 'polaris-desktop' });

  // 持久层先于 loader 直挂：配置树就存在这里。失败不阻断启动——树退化
  // 为内存 store（见下），壳仍可用；kernel.status 的 storage=false 会把
  // 这件事暴露给渲染层与冒烟测试。
  try {
    await instance.ctx.plugin(storage, {
      path: join(app.getPath('userData'), 'kernel', 'storage.db'),
    });
  } catch (err) {
    console.error('[kernel] storage 持久层挂载失败，本次会话不落盘：', err);
  }

  // storage 挂载失败时的降级：MemoryConfigTreeStore 让 loader 树照常
  // 工作（首启种子也照种），只是这次会话的改动不落盘。
  const storageSvc = instance.ctx.get('storage') as StorageService | undefined;
  const store: ConfigTreeStore = storageSvc?.configTree ?? new MemoryConfigTreeStore();

  let configTree: SqliteTree | undefined;
  try {
    if ((await store.load()).length === 0) await store.save(SEED_ENTRIES);
    await instance.ctx.plugin(Loader);
    registerBuiltins(instance.ctx.loader);
    await instance.ctx.plugin(SqliteTree, { store });
    configTree = instance.ctx.get('configTree') as SqliteTree | undefined;
  } catch (err) {
    // 树挂不上（数据损坏 / 某条目装载失败整树回滚）只损失插件能力，壳必须
    // 照常起：configTree 留空，引擎注入被跳过，前端按 localBackend=null 回落远端。
    console.error('[kernel] 配置树装载失败，本次会话无插件树：', err);
  }

  let engine = parseEngineSpec(process.env.POLARIS_DESKTOP_ENGINE);
  // 并行隔离（壳级 E2E / 同机多实例）：docker 容器名与宿主端口默认是固定值，
  // 两个实例同时跑必然撞名撞端口。这两个 env 只在显式指定引擎的测试/调试
  // 场景使用，打包态的自动引导不经过它们。
  if (engine) {
    const name = process.env.POLARIS_DESKTOP_ENGINE_CONTAINER;
    if (name) engine = { ...engine, containerName: name };
    const port = Number(process.env.POLARIS_DESKTOP_ENGINE_PORT);
    if (Number.isInteger(port) && port > 0) engine = { ...engine, port };
  }
  if (!engine && app.isPackaged) {
    engine = await bootstrapPackagedEngine();
  }
  if (engine && configTree) {
    // 引擎注入走 entry.update 而不是 tree.update：后者会把 enabled+spec
    // 写进持久树，下次启动 env 变了树还按旧答案自启引擎；entry.update 只
    // 改内存态并同步拉起 fiber，磁盘上始终是 disabled 占位——树保存用户
    // 意图（要不要这个插件），引擎参数每次启动现算注入。
    // entry.update 内部 init → fiber.await()：resolve 返回时引擎已健康或
    // 已抛错。窗口在 startKernel 之后才创建，所以 kernel.localBackend 与
    // CSP 在首个页面请求时就已是最终答案（与旧直挂语义一致）。
    const entry = configTree.store['legacy-engine'];
    if (entry) {
      try {
        await entry.update({ config: engine, disabled: null });
      } catch (err) {
        // 失败时 Entry.update 自己把 options 回滚到 disabled 且不落库，
        // 用户树不被污染；这里照旧记录错误后回落远端服务器流程。
        console.error('[kernel] 本地引擎启动失败，回落远端流程：', err);
      }
    } else {
      // 条目被用户从树里删掉：用户状态即真相，不偷偷种回去
      console.warn('[kernel] 配置树没有 legacy-engine 条目，跳过本地引擎');
    }
  } else if (engine) {
    console.error('[kernel] 配置树不可用，本地引擎无法注入，回落远端流程');
  }

  await instance.start();
  kernel = instance;
  return instance;
}

/**
 * 停机：dispose 根 fiber，级联回收（含本地引擎子进程）。幂等，未启动时是
 * no-op。落盘顺序由 disposer 的逆序执行保证：SqliteTree 比 storage 后装，
 * 先被 dispose——其 stop() 里 root.stop() 之后有最后一次 flush——storage
 * 关库排在其后，不需要在这里显式 flush。
 */
export async function stopKernel(): Promise<void> {
  const instance = kernel;
  kernel = null;
  if (instance) await instance.stop();
}

/**
 * kernel.status 的实现。plugins 用 ctx.registry.size —— cordis 公开文档口径
 * 「已注册插件 runtime 的数量」（vendor/deepseek-cordis/cordis/src/registry.ts），
 * 不数 fiber：同一插件多次装载仍算一个 runtime。树驱动后名额包括直挂的
 * storage、Loader（连带其内部 isolate）、SqliteTree，以及树条目拉起的
 * desktop-probe / sources（legacy-engine 视注入结果而定）。
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
  return bootstrapStatus;
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

/**
 * kernel.localBackend 的实现。ctx.get 是 cordis 公开的免 inject 读服务入口
 * （reflect mixin，见 vendor/deepseek-cordis/cordis/src/reflect.ts），严格模式
 * 只在提供方 fiber 处于 ACTIVE 时返回值——引擎没装、还没健康、或已失败时
 * 一律拿到 undefined，统一折叠成 null 让前端回落远端。
 */
export function localBackend(): LocalBackendInfo {
  const legacy = kernel?.ctx.get('legacy') as { baseUrl?: string } | undefined;
  return { baseUrl: legacy?.baseUrl ?? null };
}
