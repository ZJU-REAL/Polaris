/* ============================================================
   @polaris/kernel 在桌面主进程里的挂载点。

   与 agent/supervisor.ts 同样的组织方式：模块级单例 + 具名访问函数，
   不把 kernel 实例挂到全局或到处传。生命周期绑定 app：
   ready 时 start，退出路径 stop（fiber.dispose 级联回收所有插件注册
   的副作用——计时器、监听器、子进程）。

   一期内核里只有一个探针插件：不做任何事，存在本身就证明 cordis 插件
   树被真的建起来了（kernel.status 的 plugins 计数由它兜底 ≥ 1）。
   后续阶段把 config 树、进程监督、legacy engine 逐个装进来时，
   改的只有这里的装载列表。
   ============================================================ */

import { createKernel, type Kernel } from '@polaris/kernel';

import type { KernelStatus } from '../shared/contract';

let kernel: Kernel | null = null;

/** 空探针插件。命名走 kebab-case，与将来的 config-tree / legacy-engine 一致。 */
const desktopProbe = {
  name: 'desktop-probe',
  apply(): void {
    /* 故意为空：只占一个 registry 名额 */
  },
};

/** 幂等启动：重复调用返回同一实例（app ready 与 smoke 都可能触发）。 */
export async function startKernel(): Promise<Kernel> {
  if (kernel) return kernel;
  const instance = createKernel({ name: 'polaris-desktop' });
  instance.ctx.plugin(desktopProbe);
  await instance.start();
  kernel = instance;
  return instance;
}

/** 停机：dispose 根 fiber，级联回收。幂等，未启动时是 no-op。 */
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
