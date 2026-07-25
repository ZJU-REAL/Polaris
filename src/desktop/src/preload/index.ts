/* ============================================================
   Preload —— renderer 与 main 之间唯一的桥。

   这个文件是「一期写完永不再改」的那一个：它暴露的是**通道**而不是
   能力清单，所以第二期加本地能力时只改 shared/contract.ts 与 main 侧。

   暴露两个全局，职责刻意分开：
   - window.__POLARIS__  静态事实（serverUrl / platform / appVersion）。
     用 sendSync 取：lib/endpoint.ts 的 apiBase() 可能在任何时刻被调用，
     必须保证注入早于一切 renderer 脚本，异步 invoke 做不到这一点。
     一次同步 IPC 只发生在启动期，开销可忽略。
   - window.polaris      RPC 桥（invoke / subscribe）。
   ============================================================ */

import { contextBridge, ipcRenderer } from 'electron';

import {
  IPC_CHANNEL_EVENT,
  IPC_CHANNEL_INFO_SYNC,
  IPC_CHANNEL_RPC,
  type HostEvent,
  type HostInfo,
  type MethodName,
  type ParamsOf,
  type ResultOf,
} from '../shared/contract';

const info = ipcRenderer.sendSync(IPC_CHANNEL_INFO_SYNC) as HostInfo;

// 事件监听器留在 preload 侧按 id 管理：跨 contextBridge 传函数身份不可靠，
// 用 id 换取「取消订阅」的确定语义。
const listeners = new Map<number, (event: HostEvent) => void>();
let nextListenerId = 1;

ipcRenderer.on(IPC_CHANNEL_EVENT, (_e, payload: HostEvent) => {
  for (const handler of listeners.values()) {
    try {
      handler(payload);
    } catch {
      /* 单个订阅者抛错不影响其他订阅者 */
    }
  }
});

contextBridge.exposeInMainWorld('__POLARIS__', info);

contextBridge.exposeInMainWorld('polaris', {
  invoke<M extends MethodName>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
    return ipcRenderer.invoke(IPC_CHANNEL_RPC, { method, params }) as Promise<ResultOf<M>>;
  },
  subscribe(handler: (event: HostEvent) => void): number {
    const id = nextListenerId++;
    listeners.set(id, handler);
    return id;
  },
  unsubscribe(id: number): void {
    listeners.delete(id);
  },
});
