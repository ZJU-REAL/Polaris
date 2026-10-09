/* ============================================================
   首启引导的进度观测（首启等待页「看得出没卡住」）。

   uv 在非 TTY 下不画进度条，下载 Python / 装依赖时可能几十秒一行输出
   都没有，用户只看到一个转圈。这里补两样东西给 kernel.engineBootstrapStatus：
   - 输出尾巴：每行 uv 输出去 ANSI、截断后进环形缓冲，状态里带当前
     （或最近一个有输出的）阶段的最后几行；
   - 落盘字节数：python / install 阶段每 ~2 秒量一次 <dataDir>/engine
     （托管 Python、uv 缓存）相对阶段开始时的增长。uv 是边下边解压，
     这个数是解压后的体积，比网络字节略大，但「在涨」本身就是用户要的信号。

   刻意 electron-free（与 engine-bootstrap.ts 同理）：路径、时钟都由调用方传入。
   ============================================================ */

import { lstat, readdir } from 'node:fs/promises';
import { join } from 'node:path';

/** 状态里带出的日志行数。 */
export const LOG_TAIL_LINES = 8;
/** 内部环形缓冲容量（跨阶段）。 */
export const LOG_BUFFER_LINES = 200;
/** 单行截断长度（uv 偶尔打出很长的路径/依赖链）。 */
export const LOG_LINE_MAX = 200;
/** 量盘间隔：上一次量完后再等这么久，量盘本身慢也不会叠加。 */
export const SAMPLE_INTERVAL_MS = 2000;

/** 只在这两个阶段量盘：venv 几秒就完，engine 阶段不再落大文件。 */
const SAMPLED_PHASES = new Set(['python', 'install']);

/** 量盘时跳过的子目录：venv 里的文件是 uv 从缓存 clone/硬链过去的，算进来会重复计数。 */
const SKIP_DIRS = new Set(['venv']);

// CSI（颜色/光标）与 OSC（终端标题/超链接）序列
// eslint-disable-next-line no-control-regex
const ANSI_RE = /\u001b\[[0-?]*[ -/]*[@-~]|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\u001b[@-Z\\-_]/g;

/** 去 ANSI、取回车覆盖后的最后一段、去首尾空白并截断；空行返回 ''。 */
export function cleanLine(raw: string): string {
  const noAnsi = raw.replace(ANSI_RE, '');
  // 进度条式输出用 \r 原地刷新，终端上只看得到最后一段
  const segs = noAnsi.split('\r').map((s) => s.trim()).filter(Boolean);
  const line = segs.length ? segs[segs.length - 1]! : '';
  // 剩余控制字符（\t 之外）一并去掉，免得渲染成方块
  // eslint-disable-next-line no-control-regex
  const printable = line.replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '');
  return printable.length > LOG_LINE_MAX ? `${printable.slice(0, LOG_LINE_MAX - 1)}…` : printable;
}

/**
 * 目录树的文件总字节数。异步逐目录走，单次 await 只处理一个目录的条目，
 * 不会长时间占住主进程事件循环。跳过符号链接；遍历中文件/目录消失
 * （uv 的临时文件来来去去）一律当 0 处理；同一 inode 的硬链接只算一次。
 */
export async function dirSize(root: string, skipDirs: ReadonlySet<string> = new Set()): Promise<number> {
  const seen = new Set<string>();
  let total = 0;
  const walk = async (dir: string, depth: number): Promise<void> => {
    let names: string[];
    try {
      names = await readdir(dir);
    } catch {
      return;
    }
    for (const name of names) {
      if (depth === 0 && skipDirs.has(name)) continue;
      const path = join(dir, name);
      let st;
      try {
        st = await lstat(path);
      } catch {
        continue;
      }
      if (st.isSymbolicLink()) continue;
      if (st.isDirectory()) {
        await walk(path, depth + 1);
      } else if (st.isFile()) {
        if (st.nlink > 1) {
          const key = `${st.dev}:${st.ino}`;
          if (seen.has(key)) continue;
          seen.add(key);
        }
        total += st.size;
      }
    }
  };
  await walk(root, 0);
  return total;
}

/** 合进 EngineBootstrapStatus 的可选字段（老渲染层会忽略）。 */
export interface BootstrapProgressFields {
  phaseStartedAt?: number;
  log?: string[];
  lastOutputAt?: number;
  downloadedBytes?: number;
}

export interface TrackerOptions {
  /** 量盘根目录（<dataDir>/engine）。 */
  engineDir: string;
  now?: () => number;
  sampleIntervalMs?: number;
  /** 可替换的量盘函数（测试用）。 */
  measure?: (dir: string) => Promise<number>;
}

/**
 * 跟踪当前阶段、输出尾巴与落盘增长。phase() 切阶段、line() 收一行输出，
 * snapshot() 给 IPC 读；stop() 停掉采样（引导结束或失败时调用，幂等）。
 */
export class BootstrapProgressTracker {
  private readonly engineDir: string;
  private readonly now: () => number;
  private readonly interval: number;
  private readonly measure: (dir: string) => Promise<number>;

  private current = '';
  private startedAt: number | undefined;
  private lastOutputAt: number | undefined;
  private readonly buffer: { phase: string; text: string }[] = [];

  /** 采样代次：切阶段/停止时自增，旧代次的在途采样结果作废。 */
  private generation = 0;
  private baseBytes: number | undefined;
  private bytes: number | undefined;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  constructor(opts: TrackerOptions) {
    this.engineDir = opts.engineDir;
    this.now = opts.now ?? Date.now;
    this.interval = opts.sampleIntervalMs ?? SAMPLE_INTERVAL_MS;
    this.measure = opts.measure ?? ((dir) => dirSize(dir, SKIP_DIRS));
  }

  /** 进入某阶段；同一阶段重复调用是 no-op。 */
  phase(phase: string): void {
    if (phase === this.current) return;
    this.current = phase;
    this.startedAt = this.now();
    this.cancelSampling();
    this.baseBytes = undefined;
    this.bytes = undefined;
    if (!this.stopped && SAMPLED_PHASES.has(phase)) this.startSampling();
  }

  /** 收一行子进程输出；清洗后为空的行丢弃。 */
  line(raw: string): void {
    const text = cleanLine(raw);
    if (!text) return;
    this.buffer.push({ phase: this.current, text });
    if (this.buffer.length > LOG_BUFFER_LINES) this.buffer.shift();
    this.lastOutputAt = this.now();
  }

  /** 停止采样（引导结束/失败）。已采到的字节数保留在快照里。 */
  stop(): void {
    this.stopped = true;
    this.cancelSampling();
  }

  snapshot(): BootstrapProgressFields {
    const out: BootstrapProgressFields = {};
    if (this.startedAt !== undefined) out.phaseStartedAt = this.startedAt;
    if (this.lastOutputAt !== undefined) out.lastOutputAt = this.lastOutputAt;
    // 当前阶段有输出就给当前阶段的；没有（如 engine 阶段）给最近一个有输出的阶段
    let tailPhase = this.current;
    if (!this.buffer.some((l) => l.phase === tailPhase) && this.buffer.length) {
      tailPhase = this.buffer[this.buffer.length - 1]!.phase;
    }
    const lines = this.buffer.filter((l) => l.phase === tailPhase).map((l) => l.text);
    out.log = lines.slice(-LOG_TAIL_LINES);
    if (this.bytes !== undefined) out.downloadedBytes = this.bytes;
    return out;
  }

  private startSampling(): void {
    const gen = ++this.generation;
    const tick = async (): Promise<void> => {
      let size: number;
      try {
        size = await this.measure(this.engineDir);
      } catch {
        size = NaN;
      }
      if (gen !== this.generation) return;
      if (Number.isFinite(size)) {
        if (this.baseBytes === undefined) this.baseBytes = size;
        this.bytes = Math.max(0, size - this.baseBytes);
      }
      this.timer = setTimeout(() => void tick(), this.interval);
      // 采样不该拖住进程退出（bootstrap-smoke 是纯 node 脚本）
      (this.timer as { unref?: () => void }).unref?.();
    };
    void tick();
  }

  private cancelSampling(): void {
    this.generation++;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}
