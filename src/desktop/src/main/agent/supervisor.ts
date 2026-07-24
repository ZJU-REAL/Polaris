/* ============================================================
   本地 agent 进程的生命周期。

   为什么用 child_process 而不是 Electron 的 utilityProcess：
   utilityProcess 不支持 stdin 管道，而这层设计的全部意义就是「行分隔 JSON-RPC
   over stdio、将来可以原地换成一个 Python 进程」。用 child_process 拿到真正的
   双向 stdio，换实现时只需要换一条命令。

   Node 侧靠 ELECTRON_RUN_AS_NODE 复用 Electron 自带的 Node 运行时——打包后的
   应用里没有独立的 node 可执行文件。

   一期 agent 只实现 initialize/ping，其余方法一律 method not found，于是所有
   local.* 都会以 ERR_CAPABILITY_UNAVAILABLE 结束。但管道是真的通的：
   renderer → preload → router → supervisor → agent → 回来。第二期接本地能力时，
   改的只有 agent 侧的 handler。
   ============================================================ */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { join } from 'node:path';

import { JsonRpcClient } from './rpc';

const MAX_RESTARTS = 3;

let child: ChildProcessWithoutNullStreams | null = null;
let client: JsonRpcClient | null = null;
let restarts = 0;
let lastError: string | null = null;

function agentEntry(): string {
  return join(__dirname, 'agent.cjs');
}

function start(): void {
  child = spawn(process.execPath, [agentEntry()], {
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  client = new JsonRpcClient(child.stdin, child.stdout);

  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk: string) => console.error('[polaris-locald]', chunk.trimEnd()));

  child.on('exit', (code, signal) => {
    lastError = `agent exited (code=${code ?? 'null'} signal=${signal ?? 'null'})`;
    client?.rejectAll(lastError);
    child = null;
    client = null;
  });

  child.on('error', (err) => {
    lastError = err.message;
    client?.rejectAll(err.message);
    child = null;
    client = null;
  });
}

/** 懒启动 + 有限次重启。返回 null 表示 agent 起不来。 */
function ensure(): JsonRpcClient | null {
  if (client) return client;
  if (restarts >= MAX_RESTARTS) return null;
  restarts += 1;
  try {
    start();
  } catch (err) {
    lastError = err instanceof Error ? err.message : String(err);
    return null;
  }
  return client;
}

export function agentUnavailableReason(): string | null {
  return client ? null : (lastError ?? 'agent not started');
}

/** 调用 agent 的一个方法；agent 起不来时抛错（由 router 转成能力不可用）。 */
export async function callAgent(method: string, params?: unknown): Promise<unknown> {
  const rpc = ensure();
  if (!rpc) throw new Error(lastError ?? 'local agent unavailable');
  return await rpc.call(method, params);
}

/** 探活：一期用来证明这条管道真的通。 */
export async function pingAgent(): Promise<boolean> {
  try {
    return (await callAgent('ping')) === 'pong';
  } catch {
    return false;
  }
}

export function stopAgent(): void {
  child?.kill();
  child = null;
  client = null;
}
