/* ============================================================
   @polaris/kernel 在桌面主进程里的挂载点。

   与 agent/supervisor.ts 同样的组织方式：模块级单例 + 具名访问函数，
   不把 kernel 实例挂到全局或到处传。生命周期绑定 app：
   ready 时 start，退出路径 stop（fiber.dispose 级联回收所有插件注册
   的副作用——计时器、监听器、子进程）。

   装载列表：
   - desktop-probe：空探针，存在本身就证明 cordis 插件树被真的建起来了
     （kernel.status 的 plugins 计数由它兜底 ≥ 1）。
   - legacy-engine：把 Python 后端作为本地子进程拉起（desktop 档位单进程，
     见 #583）。配置来源按优先级：
       1. POLARIS_DESKTOP_ENGINE 显式指定（开发/调试，行为与从前完全一致）
       2. 打包态自动引导（#607）：安装包自带 uv + 后端源码，首启在 userData
          里装出 venv 后以 command 模式拉起——用户机器不需要 Python/docker
     两者都不成立（开发态未设 env）时不装引擎，保持远端服务器流程。
   ============================================================ */

import { app } from 'electron';

import { createKernel, legacyEngine, type Kernel, type LegacyEngineConfig } from '@polaris/kernel';

import type { EngineBootstrapStatus, KernelStatus, LocalBackendInfo } from '../shared/contract';
import { bootstrapEngine } from './engine-bootstrap';

let kernel: Kernel | null = null;

/**
 * 内嵌引擎引导进度，kernel.engineBootstrapStatus 直接读它。
 * idle = 没走内嵌路径（开发态 / 显式 env）；failed = 引导失败已回落远端。
 * 渲染层的进度条二期再接，这里先把契约与状态源立起来。
 */
let bootstrapStatus: EngineBootstrapStatus = { phase: 'idle', done: false };

/** 空探针插件。命名走 kebab-case，与 config-tree / legacy-engine 一致。 */
const desktopProbe = {
  name: 'desktop-probe',
  apply(): void {
    /* 故意为空：只占一个 registry 名额 */
  },
};

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
  instance.ctx.plugin(desktopProbe);

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
  if (engine) {
    // 等到健康（或失败）再返回：窗口在 startKernel 之后才创建，这样
    // kernel.localBackend 与 CSP 在首个页面请求时就已是最终答案，渲染层
    // 不用处理「先远端后本地」的中途切换。失败不阻断启动——记录错误后
    // 回落远端服务器流程（localBackend 返回 null）。
    try {
      await instance.ctx.plugin(legacyEngine, engine);
    } catch (err) {
      console.error('[kernel] 本地引擎启动失败，回落远端流程：', err);
    }
  }

  await instance.start();
  kernel = instance;
  return instance;
}

/** 停机：dispose 根 fiber，级联回收（含本地引擎子进程）。幂等，未启动时是 no-op。 */
export async function stopKernel(): Promise<void> {
  const instance = kernel;
  kernel = null;
  if (instance) await instance.stop();
}

/**
 * kernel.status 的实现。plugins 用 ctx.registry.size —— cordis 公开文档口径
 * 「已注册插件 runtime 的数量」（vendor/deepseek-cordis/cordis/src/registry.ts），
 * 不数 fiber：同一插件多次装载仍算一个 runtime，作为「插件树活着」的指标更稳。
 */
export function kernelStatus(): KernelStatus {
  return {
    started: kernel?.started ?? false,
    name: kernel?.name ?? '',
    plugins: kernel ? kernel.ctx.registry.size : 0,
  };
}

/** kernel.engineBootstrapStatus 的实现：内嵌引擎引导进度（诊断/进度条用）。 */
export function engineBootstrapStatus(): EngineBootstrapStatus {
  return bootstrapStatus;
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
