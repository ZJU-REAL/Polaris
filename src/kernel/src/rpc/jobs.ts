/* ============================================================
   长任务（job）登记与进度广播，与传输无关（#754）。

   登记表本身是纯簿记：谁在跑、能不能取消。真正跟传输绑死的只有一件事——
   把事件送到订阅者手上。桌面是 webContents.send，服务器是 SSE/WS 广播，
   所以 emit 做成构造参数，其余照搬。

   为什么一期就要有 job 而不是让 invoke 直接等：安装要下载、校验、解压、
   登记四步，前端要能显示进度；把它做成 request/response 就只剩下「转圈」，
   而且第二期真要进度时 preload 与订阅代码得整体返工。
   ============================================================ */

import { randomUUID } from 'node:crypto'

/** 事件的最小形状；具体事件类型由各形态的契约声明。 */
export interface JobEvent {
  type: string
  [key: string]: unknown
}

interface Job {
  id: string
  kind: string
  cancel?: () => void
}

export class JobBus {
  private readonly jobs = new Map<string, Job>()

  constructor(private readonly emit: (event: JobEvent) => void) {}

  start(kind: string, cancel?: () => void): string {
    const id = randomUUID()
    this.jobs.set(id, { id, kind, cancel })
    return id
  }

  progress(jobId: string, phase: string, done: number, total: number, note?: string): void {
    this.emit({ type: 'job.progress', jobId, phase, done, total, note })
  }

  log(jobId: string, chunk: string): void {
    this.emit({ type: 'job.log', jobId, chunk })
  }

  finish(jobId: string, result: unknown): void {
    this.jobs.delete(jobId)
    this.emit({ type: 'job.done', jobId, result })
  }

  fail(jobId: string, code: string, message: string): void {
    this.jobs.delete(jobId)
    this.emit({ type: 'job.error', jobId, code, message })
  }

  cancel(jobId: string): void {
    const job = this.jobs.get(jobId)
    if (!job) return
    job.cancel?.()
    this.jobs.delete(jobId)
  }
}
