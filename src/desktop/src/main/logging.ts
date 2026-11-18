/* ============================================================
   日志落盘：<userData>/logs/ 下三个文件，都按大小滚动、写入前抹掉密钥
   （实现在 @polaris/kernel 的 log/rotating-log.ts）。

   - main.log       主进程的 console.log / warn / error（内核、更新、窗口）
   - bootstrap.log  首启安装时 uv 的全部输出，加上引导自己的说明
                    （用了哪个下载源、重试了几次、为什么失败）
   - engine.log     本机引擎（Python）的 stdout / stderr，由 legacy-engine
                    插件直接写（路径经配置交给它）

   以前这些全都只进一个用户看不到的控制台：出了问题用户没有任何东西可以
   发给我们，我们也无从判断。现在「设置 → 关于」和安装失败页都能打开这个
   文件夹、复制一份诊断信息。

   每个文件 2 MB 封顶、留 3 份历史（最多约 8 MB），不会无限长大。
   ============================================================ */

import { app, shell } from 'electron';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { format } from 'node:util';

import { RotatingLog, scrubProcessSecrets, tailLogFile } from '@polaris/kernel';

export function logsDir(): string {
  return join(app.getPath('userData'), 'logs');
}

export function engineLogPath(): string {
  return join(logsDir(), 'engine.log');
}

export function bootstrapLogPath(): string {
  return join(logsDir(), 'bootstrap.log');
}

export function mainLogPath(): string {
  return join(logsDir(), 'main.log');
}

let mainLog: RotatingLog | null = null;
let bootstrapLog: RotatingLog | null = null;

/**
 * 把主进程的 console 输出同时写进 main.log。必须在 userData 定下来之后、
 * 其余模块开始打日志之前调用。重复调用是 no-op。
 */
export function installMainLog(): void {
  if (mainLog) return;
  const log = new RotatingLog({ path: mainLogPath() });
  mainLog = log;
  for (const level of ['log', 'info', 'warn', 'error'] as const) {
    const original = console[level].bind(console);
    console[level] = (...args: unknown[]): void => {
      original(...args);
      try {
        const tag = level === 'log' || level === 'info' ? '' : `[${level}] `;
        log.write(`${tag}${format(...args)}`);
      } catch {
        /* 日志写不进去不该影响应用 */
      }
    };
  }
  // 只旁听不接管：未捕获异常仍按 Electron 的默认方式处理，这里留一份记录
  process.on('uncaughtExceptionMonitor', (err) => {
    log.write(`[fatal] uncaught exception: ${err?.stack ?? String(err)}`);
  });
  process.on('unhandledRejection', (reason) => {
    log.write(`[error] unhandled rejection: ${reason instanceof Error ? (reason.stack ?? reason.message) : String(reason)}`);
  });
  log.write(
    `---- Polaris ${app.getVersion()} starting (electron ${process.versions.electron}, ${process.platform} ${process.arch}) ----`,
  );
}

/** 首启安装的一行输出 → bootstrap.log。 */
export function writeBootstrapLog(line: string): void {
  bootstrapLog ??= new RotatingLog({ path: bootstrapLogPath() });
  bootstrapLog.write(line);
}

/** 在文件管理器里打开日志文件夹（没有就先建出来）。 */
export async function openLogsFolder(): Promise<boolean> {
  const dir = logsDir();
  try {
    mkdirSync(dir, { recursive: true });
  } catch {
    /* 建不出来时 openPath 会如实返回错误 */
  }
  const error = await shell.openPath(dir);
  if (error) console.error(`[logging] 打不开日志文件夹：${error}`);
  return !error;
}

/** 某个日志文件的最后几行（已是抹过密钥的落盘内容）。 */
export function logTail(path: string, lines: number): string[] {
  return tailLogFile(path, lines);
}

/** 给即将离开主进程的文字（诊断信息、失败页上的错误行）再过一遍，防止内存里的原文带出密钥。 */
export function scrub(text: string): string {
  return scrubProcessSecrets(text);
}
