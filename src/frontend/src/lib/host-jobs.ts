/* ============================================================
   本地长任务的流式辅助。

   形态刻意抄 lib/sse.ts 的 subscribeSse / postSse：团队已经习惯「传一组
   handlers、拿一个取消函数」这套心智，本地任务没必要另发明一种。

   为什么一期就要有：编译日志、目录扫描进度都是流式的。如果一期只有
   request/response 的 invoke，第二期要么被迫轮询，要么临时开新通道——
   后者意味着 preload 与所有前端 hook 返工。
   ============================================================ */

import { invokeHost, onHostEvent } from './host';

export interface JobHandlers {
  onProgress?: (p: { phase: string; done: number; total: number; note?: string }) => void;
  onLog?: (chunk: string) => void;
  onDone?: (result: unknown) => void;
  onError?: (code: string, message: string) => void;
}

/**
 * 调起一个本地长任务：invoke 立即返回 { jobId }，后续进度经事件通道推。
 * 返回取消函数（会通知主进程取消，并停止本地回调）。
 */
export function invokeJob(method: string, params: unknown, handlers: JobHandlers): () => void {
  let jobId: string | null = null;
  let stopped = false;
  // 事件可能早于 invoke 的 resolve 到达，所以先订阅、拿到 jobId 后再过滤
  const buffered: { type: string; payload: unknown }[] = [];

  const unsubscribe = onHostEvent((event) => {
    if (stopped) return;
    if (!('jobId' in event)) return;
    if (jobId === null) {
      buffered.push({ type: event.type, payload: event });
      return;
    }
    if (event.jobId !== jobId) return;
    deliver(event);
  });

  function deliver(event: Extract<Parameters<Parameters<typeof onHostEvent>[0]>[0], { jobId: string }>): void {
    switch (event.type) {
      case 'job.progress':
        handlers.onProgress?.({
          phase: event.phase,
          done: event.done,
          total: event.total,
          note: event.note,
        });
        break;
      case 'job.log':
        handlers.onLog?.(event.chunk);
        break;
      case 'job.done':
        handlers.onDone?.(event.result);
        stop();
        break;
      case 'job.error':
        handlers.onError?.(event.code, event.message);
        stop();
        break;
    }
  }

  function stop(): void {
    if (stopped) return;
    stopped = true;
    unsubscribe();
  }

  void invokeHost(method, params).then(
    (result) => {
      if (stopped) return;
      jobId = (result as { jobId?: string })?.jobId ?? null;
      if (jobId === null) {
        handlers.onError?.('ERR_NO_JOB_ID', 'host did not return a jobId');
        stop();
        return;
      }
      for (const item of buffered) {
        const event = item.payload as { jobId: string };
        if (event.jobId === jobId) deliver(event as never);
      }
      buffered.length = 0;
    },
    (err: unknown) => {
      if (stopped) return;
      handlers.onError?.('ERR_INVOKE_FAILED', err instanceof Error ? err.message : String(err));
      stop();
    },
  );

  return () => {
    if (jobId) void invokeHost('local.job.cancel', { jobId }).catch(() => {});
    stop();
  };
}
