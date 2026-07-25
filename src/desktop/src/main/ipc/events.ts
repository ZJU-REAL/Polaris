/* 事件广播与长任务（job）生命周期。

   为什么一期就要有：编译日志、目录扫描进度天然是流式的。如果一期只有
   request/response 的 invoke，第二期要么被迫轮询，要么临时开一条新通道——
   后者意味着 preload 与前端订阅代码全部返工。 */

import { randomUUID } from 'node:crypto';

import { IPC_CHANNEL_EVENT, type HostEvent } from '../../shared/contract';
import { getWindow } from '../window';

export function emit(event: HostEvent): void {
  const win = getWindow();
  if (win && !win.isDestroyed()) win.webContents.send(IPC_CHANNEL_EVENT, event);
}

interface Job {
  id: string;
  kind: string;
  cancel?: () => void;
}

const jobs = new Map<string, Job>();

export function startJob(kind: string, cancel?: () => void): string {
  const id = randomUUID();
  jobs.set(id, { id, kind, cancel });
  return id;
}

export function progress(jobId: string, phase: string, done: number, total: number, note?: string): void {
  emit({ type: 'job.progress', jobId, phase, done, total, note });
}

export function log(jobId: string, chunk: string): void {
  emit({ type: 'job.log', jobId, chunk });
}

export function finish(jobId: string, result: unknown): void {
  jobs.delete(jobId);
  emit({ type: 'job.done', jobId, result });
}

export function fail(jobId: string, code: string, message: string): void {
  jobs.delete(jobId);
  emit({ type: 'job.error', jobId, code, message });
}

export function cancelJob(jobId: string): void {
  const job = jobs.get(jobId);
  if (!job) return;
  job.cancel?.();
  jobs.delete(jobId);
}
