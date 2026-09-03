/* ============================================================
   @polaris/kernel 在桌面主进程里的挂载点。

   与 agent/supervisor.ts 同样的组织方式：模块级单例 + 具名访问函数，
   不把 kernel 实例挂到全局或到处传。生命周期绑定 app：
   ready 时 start，退出路径 stop（fiber.dispose 级联回收所有插件注册
   的副作用——计时器、监听器、子进程）。

   装载列表：
   - desktop-probe：空探针，存在本身就证明 cordis 插件树被真的建起来了
     （kernel.status 的 plugins 计数由它兜底 ≥ 1）。
   - legacy-engine：仅当设置了 POLARIS_DESKTOP_ENGINE 时装载，把 Python
     后端作为本地子进程拉起（desktop 档位单进程，见 #583）；未设置时
     保持现有的远端服务器流程，一行不变。
   ============================================================ */

import { createKernel, legacyEngine, type Kernel, type LegacyEngineConfig } from '@polaris/kernel';

import type { KernelStatus, LocalBackendInfo } from '../shared/contract';

let kernel: Kernel | null = null;

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
 * 未设置 / 解析失败 = 不装本地引擎。
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

/** 幂等启动：重复调用返回同一实例（app ready 与 smoke 都可能触发）。 */
export async function startKernel(): Promise<Kernel> {
  if (kernel) return kernel;
  const instance = createKernel({ name: 'polaris-desktop' });
  instance.ctx.plugin(desktopProbe);

  const engine = parseEngineSpec(process.env.POLARIS_DESKTOP_ENGINE);
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
