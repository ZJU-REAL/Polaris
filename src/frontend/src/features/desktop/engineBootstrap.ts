/* ============================================================
   首启等待页的状态机（纯函数，vitest 直测）。

   桌面端窗口先于内核创建（#721）：打包态首启要在后台下载 Python、装依赖、
   起引擎，短则秒级长则数分钟。挂载前 main.tsx 问一次引导状态，未就绪就先
   进等待页轮询；这里只做「状态 → 决策」的纯映射，轮询与副作用在组件里。
   ============================================================ */

import type { EngineBootstrapStatus } from '../../lib/host';

export type BootstrapGate = 'proceed' | 'wait' | 'failed';

/**
 * 由引导状态决定挂载去向：
 * - null（web 端 / 旧宿主 / 桥故障）→ 放行，按既有远端流程走；
 * - idle = 没走内嵌路径（开发态 / 显式 env）→ 放行。旧宿主的 idle 可能
 *   done=false（当年初始值如此），也一并放行——旧宿主在窗口前就启动完了；
 * - failed → 失败页（给「使用远程服务器」出路）；
 * - 其余按 done 判断：没完就等。
 */
export function bootstrapGate(status: EngineBootstrapStatus | null): BootstrapGate {
  if (status == null) return 'proceed';
  if (status.phase === 'failed') return 'failed';
  if (status.phase === 'idle') return 'proceed';
  return status.done ? 'proceed' : 'wait';
}

/** 等待页展示的四个阶段（大白话，不用内部术语）。 */
export const BOOTSTRAP_STEPS = [
  { zh: '下载 Python', en: 'Downloading Python' },
  { zh: '创建运行环境', en: 'Creating the environment' },
  { zh: '安装组件', en: 'Installing components' },
  { zh: '启动引擎', en: 'Starting the engine' },
] as const;

/**
 * phase → 高亮的阶段序号；-1 = 还没进入具名阶段（starting/check：内核
 * 启动中或复用旧环境的快速检查，几秒内就会有结论）。
 * ready 归入「启动引擎」：done 之前它只是引导脚本的收尾态。
 */
export function bootstrapStepIndex(phase: string): number {
  switch (phase) {
    case 'python':
      return 0;
    case 'venv':
      return 1;
    case 'install':
      return 2;
    case 'engine':
    case 'ready':
      return 3;
    default:
      return -1;
  }
}
