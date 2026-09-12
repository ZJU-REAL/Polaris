/* ============================================================
   唯一的 IPC 入口：ipcMain.handle('polaris:rpc')。

   所有方法在这里集中做参数校验后再分发。刻意不引 zod —— 一期只有 6 个方法、
   参数都是单个字符串或数字，手写守卫比多一个运行时依赖清楚；真正重要的是
   「校验发生在唯一一处」这个结构，而不是用哪个库。

   注意信任边界：renderer 里的 JS 来自服务器返回的数据渲染而成，所以这里的
   参数一律当作不可信输入独立校验，不因为「前端已经检查过」而省略。
   ============================================================ */

import { ipcMain } from 'electron';

import {
  ERR_INVALID_PARAMS,
  ERR_UNKNOWN_METHOD,
  IPC_CHANNEL_INFO_SYNC,
  IPC_CHANNEL_RPC,
  type MethodName,
  type RpcRequest,
} from '../../shared/contract';
import { capabilityManifest } from '../capabilities';
import { engineBootstrapStatus, kernelStatus, localBackend } from '../kernel';
import { applyUpdate, checkForUpdate } from '../updates';
import { cancelJob } from './events';
import * as host from './methods.host';
import * as market from './methods.market';
import * as plugins from './methods.plugins';

function asString(params: unknown, key: string): string {
  const value = (params as Record<string, unknown> | null)?.[key];
  if (typeof value !== 'string') {
    throw new Error(`${ERR_INVALID_PARAMS}: ${key} must be a string`);
  }
  return value;
}

function asNumber(params: unknown, key: string): number {
  const value = (params as Record<string, unknown> | null)?.[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${key} must be a finite number`);
  }
  return value;
}

type Handler = (params: unknown) => unknown | Promise<unknown>;

const HANDLERS: Record<MethodName, Handler> = {
  // plugins.*：守卫与语义都在 kernel 的 createPluginMethods 里（#754），两种
  // 传输共用同一份；能力门槛（树不可达 → ERR_CAPABILITY_UNAVAILABLE）也在那里。
  // 摊在最前面：下面的具名键是桌面独有的，任何重名都该以具名的为准。
  ...plugins.pluginMethods,
  // plugins.market.*（#708）：守卫与语义同样在 kernel（#754）；包名/版本的
  // 语义校验本来就在安装引擎里，这一层只管形状
  ...market.marketMethods,
  'host.info': () => host.hostInfo(),
  'host.setServerUrl': (p) => host.setServerUrl(asString(p, 'url')),
  'host.testServer': (p) => host.testServer(asString(p, 'url')),
  'host.openExternal': (p) => host.openExternal(asString(p, 'url')),
  'host.copyText': (p) => host.copyText(asString(p, 'text')),
  'host.setBadgeCount': (p) => host.setBadgeCount(asNumber(p, 'count')),
  'host.capabilities': () => capabilityManifest(),
  'host.update.check': () => checkForUpdate(),
  'host.update.apply': () => applyUpdate(),
  'kernel.status': () => kernelStatus(),
  'kernel.localBackend': () => localBackend(),
  'kernel.engineBootstrapStatus': () => engineBootstrapStatus(),
  // 取消是 main 内的簿记（events.ts 的 job 注册表），不涉及任何外部进程
  'local.job.cancel': (p) => cancelJob(asString(p, 'jobId')),
};

export function installIpc(): void {
  // preload 用 sendSync 取静态事实，必须早于一切 renderer 脚本
  ipcMain.on(IPC_CHANNEL_INFO_SYNC, (event) => {
    event.returnValue = host.hostInfo();
  });

  ipcMain.handle(IPC_CHANNEL_RPC, async (_event, request: RpcRequest) => {
    const method = request?.method;
    const handler = typeof method === 'string' ? HANDLERS[method as MethodName] : undefined;
    if (!handler) throw new Error(`${ERR_UNKNOWN_METHOD}: ${String(method)}`);
    return await handler(request.params);
  });
}
