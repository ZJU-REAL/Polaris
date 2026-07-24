/* local.* 的一期实现：全部沿「main → agent」这条真实管道走一遍，然后以
   ERR_CAPABILITY_UNAVAILABLE 结束。

   刻意不在这里直接 throw：如果一期在 main 内部就地抛错，第二期把实现搬到 agent
   进程时，错误传播、超时、重启语义全部要重做。现在先让请求真的穿过 stdio 到
   agent 再回来，第二期改的就只有 agent 侧的 handler。 */

import { ERR_CAPABILITY_UNAVAILABLE } from '../../shared/contract';
import { callAgent } from '../agent/supervisor';
import { cancelJob } from './events';

async function viaAgent(method: string, params: unknown): Promise<never> {
  let detail: string;
  try {
    await callAgent(method, params);
    // agent 一期不可能成功返回这些方法；真返回了说明契约错位，同样按不可用处理
    detail = 'agent reported success for a phase-1 stub';
  } catch (err) {
    detail = err instanceof Error ? err.message : String(err);
  }
  throw new Error(`${ERR_CAPABILITY_UNAVAILABLE}: ${method} — ${detail}`);
}

export function latexCompile(params: unknown): Promise<never> {
  return viaAgent('latex.compile', params);
}

export function pickFolder(params: unknown): Promise<never> {
  return viaAgent('fs.pickFolder', params);
}

export function papersScan(params: unknown): Promise<never> {
  return viaAgent('papers.scan', params);
}

/** 取消是本地簿记，不需要 agent 参与——一期就能正确工作。 */
export function jobCancel(jobId: string): void {
  cancelJob(jobId);
}
