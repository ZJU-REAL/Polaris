/* 事件广播与长任务（job）生命周期。

   为什么一期就要有：编译日志、目录扫描进度天然是流式的。如果一期只有
   request/response 的 invoke，第二期要么被迫轮询，要么临时开一条新通道——
   后者意味着 preload 与前端订阅代码全部返工。 */

import { JobBus, type JobEvent } from '@polaris/kernel';

import { IPC_CHANNEL_EVENT, type HostEvent } from '../../shared/contract';
import { getWindow } from '../window';

export function emit(event: HostEvent): void {
  const win = getWindow();
  if (win && !win.isDestroyed()) win.webContents.send(IPC_CHANNEL_EVENT, event);
}

/**
 * job 登记表住在 kernel（#754，服务器形态用同一份），桌面只提供落点：
 * 事件经 webContents 推给渲染进程。
 */
export const jobBus = new JobBus((event: JobEvent) => emit(event as HostEvent));

export function startJob(kind: string, cancel?: () => void): string {
  return jobBus.start(kind, cancel);
}

export function progress(jobId: string, phase: string, done: number, total: number, note?: string): void {
  jobBus.progress(jobId, phase, done, total, note);
}

export function log(jobId: string, chunk: string): void {
  jobBus.log(jobId, chunk);
}

export function finish(jobId: string, result: unknown): void {
  jobBus.finish(jobId, result);
}

export function fail(jobId: string, code: string, message: string): void {
  jobBus.fail(jobId, code, message);
}

export function cancelJob(jobId: string): void {
  jobBus.cancel(jobId);
}
