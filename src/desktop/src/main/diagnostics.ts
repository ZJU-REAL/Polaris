/* ============================================================
   诊断信息：一份可以直接贴进 issue 的纯文本，加上兜底页要显示的那几项。

   内容：版本（外壳 / 界面 / 引擎）、系统、引擎状态与失败原因、最近 200 行
   引擎日志；安装没完成时再附上安装日志的尾巴。所有文字离开主进程前都过一遍
   抹密钥（日志文件本身落盘时已经抹过，这里是防内存里的原文）。
   ============================================================ */

import { app } from 'electron';
import { arch, release, type } from 'node:os';

import type { Diagnostics } from '../shared/contract';
import {
  engineBootstrapStatus,
  engineFailureInfo,
  engineLogTailInMemory,
  engineRunState,
  localBackend,
} from './kernel';
import { bootstrapLogPath, engineLogPath, logTail, logsDir, scrub } from './logging';
import { effectiveVersion } from './updates/renderer-store';
import { installedBackendHash } from './updates/engine-compat';

const ENGINE_LOG_LINES = 200;
const BOOTSTRAP_LOG_LINES = 60;

function osLabel(): string {
  // process.getSystemVersion 给的是用户认得的系统版本（macOS 15.1），os.release 是内核版本
  const system = typeof process.getSystemVersion === 'function' ? process.getSystemVersion() : release();
  return `${type()} ${system} (${process.platform} ${arch()})`;
}

/** 引擎自报的版本（/api/health）。引擎没在跑、或两秒内没应答，返回 null。 */
export async function engineVersion(): Promise<string | null> {
  const { baseUrl } = localBackend();
  if (!baseUrl) return null;
  try {
    const res = await fetch(`${baseUrl}/api/health`, { signal: AbortSignal.timeout(2_000) });
    const body = (await res.json()) as { version?: unknown };
    return typeof body.version === 'string' ? body.version : null;
  } catch {
    return null;
  }
}

export async function diagnostics(): Promise<Diagnostics> {
  const { failure, degraded } = engineFailureInfo();
  const status = engineBootstrapStatus();
  const engineState = engineRunState();
  const appVersion = app.getVersion();
  const uiVersion = effectiveVersion();
  const version = await engineVersion();
  const os = osLabel();
  const dataDir = app.getPath('userData');
  const backendHash = installedBackendHash();

  let engineLines = logTail(engineLogPath(), ENGINE_LOG_LINES);
  if (engineLines.length === 0) engineLines = engineLogTailInMemory().slice(-ENGINE_LOG_LINES);
  const bootstrapLines = status.phase === 'ready' || status.phase === 'idle' ? [] : logTail(bootstrapLogPath(), BOOTSTRAP_LOG_LINES);

  const text = [
    `Polaris ${appVersion}${uiVersion !== appVersion ? ` (interface ${uiVersion})` : ''}`,
    `Engine: ${version ?? 'not running'}${backendHash ? ` (build ${backendHash.slice(0, 12)})` : ''}`,
    `Engine state: ${engineState}; setup phase: ${status.phase}`,
    `System: ${os}`,
    `Electron ${process.versions.electron}, Chrome ${process.versions.chrome}, Node ${process.versions.node}`,
    `Packaged: ${app.isPackaged ? 'yes' : 'no (running from source)'}`,
    `Data folder: ${dataDir}`,
    `Logs folder: ${logsDir()}`,
    ...(degraded ? [`Note: the updated environment could not be prepared; running on the previous one (${degraded})`] : []),
    ...(failure
      ? ['', `Last failure (${failure.code}, ${new Date(failure.at).toISOString()}): ${failure.message}`, ...failure.lines.map((l) => `  ${l}`)]
      : []),
    ...(bootstrapLines.length ? ['', `---- setup log (last ${bootstrapLines.length} lines) ----`, ...bootstrapLines] : []),
    '',
    `---- engine log (last ${engineLines.length} lines) ----`,
    ...(engineLines.length ? engineLines : ['(empty)']),
  ]
    .map((line) => scrub(line))
    .join('\n');

  return {
    appVersion,
    uiVersion,
    engineVersion: version,
    os,
    engineState,
    bootstrapPhase: status.phase,
    failure,
    degraded,
    logsDir: logsDir(),
    dataDir,
    text,
  };
}
