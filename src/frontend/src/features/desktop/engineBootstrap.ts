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
 * - null（web 端 / 旧宿主 / 桥故障）→ 放行，交给 App 判断有没有本机引擎；
 * - idle = 没走内嵌路径（开发态 / 显式 env）→ 放行。旧宿主的 idle 可能
 *   done=false（当年初始值如此），也一并放行——旧宿主在窗口前就启动完了；
 * - failed → 失败页（给「重新打开」出路）；
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

/* —— 进度细节（新宿主才有的可选字段，老宿主一律不显示） —— */

/** 无输出、无落盘增长超过这么久，才提示「仍在进行」。 */
export const STALL_THRESHOLD_MS = 90_000;

/** 字节数 → 简短可读（1024 进制）：512 KB / 8.4 MB / 236 MB / 1.2 GB。 */
export function formatBytes(bytes: number): string {
  const b = Math.max(0, bytes);
  const KB = 1024;
  const MB = KB * 1024;
  const GB = MB * 1024;
  if (b >= GB) return `${(b / GB).toFixed(1)} GB`;
  if (b >= MB) {
    const mb = b / MB;
    return mb >= 10 ? `${Math.round(mb)} MB` : `${mb.toFixed(1)} MB`;
  }
  // 不足 1 KB 但非零也显示 1 KB：「在涨」比精确更要紧
  return `${b === 0 ? 0 : Math.max(1, Math.round(b / KB))} KB`;
}

/** 已用时长：中文「1 分 20 秒」「45 秒」「1 小时 2 分」，英文「1:20」「0:45」「1:02:03」。 */
export function formatElapsed(ms: number, lang: 'zh' | 'en'): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (lang === 'en') {
    const pad = (n: number) => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }
  if (h > 0) return `${h} 小时 ${m} 分`;
  if (m > 0) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

/**
 * 是否该提示「仍在进行」：阶段开始、最近一行输出、最近一次看到字节数增长
 * 三者中最晚的那个距今超过阈值。没有 phaseStartedAt（老宿主）不提示——
 * 拿不到可靠的起点，宁可不说。
 */
export function isStalled(
  activity: { phaseStartedAt?: number; lastOutputAt?: number; lastGrowthAt?: number },
  now: number,
  thresholdMs: number = STALL_THRESHOLD_MS,
): boolean {
  if (activity.phaseStartedAt == null) return false;
  const last = Math.max(activity.phaseStartedAt, activity.lastOutputAt ?? 0, activity.lastGrowthAt ?? 0);
  return now - last > thresholdMs;
}
