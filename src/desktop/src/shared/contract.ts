/* ============================================================
   Main ↔ renderer 的唯一契约。

   刻意做成「单通道 + 方法表」而不是每个能力一个 ipcMain.handle：
   preload 是打进安装包、renderer 直接可见的边界，一旦按能力开通道，
   将来每加一个本地能力都要同时改 preload + main + renderer 三处。
   单通道让 preload 写完就不再改——加方法只动本文件与 main 侧实现。

   方法命名：<域>.<对象>.<动词>。
   host.*  = 外壳能力，桌面端恒可用。
   local.* = 本地计算能力（第二期），届时只往 Methods 里加条目。
   ============================================================ */

/** preload 同步注入到 window.__POLARIS__ 的静态事实（见 preload/index.ts 的说明）。 */
export interface HostInfo {
  /** 已配置的服务器地址；未配置时为空串（前端据此进入首启配置页）。 */
  serverUrl: string;
  platform: 'darwin' | 'win32' | 'linux';
  appVersion: string;
}

/** 服务器连通性探测结果（打 GET {url}/api/health）。 */
export type ServerProbe =
  | { ok: true; version: string }
  | { ok: false; reason: 'invalid-url' | 'unreachable' | 'timeout' | 'not-polaris'; detail?: string };

export interface Methods {
  'host.info': { params: void; result: HostInfo };
  /** 保存服务器地址并重建窗口（刷新 preload 注入值与 CSP）。 */
  'host.setServerUrl': { params: { url: string }; result: void };
  /** 探测服务器地址是否可用；不写入配置。 */
  'host.testServer': { params: { url: string }; result: ServerProbe };
  /** 用系统浏览器打开外链（仅 http/https）。 */
  'host.openExternal': { params: { url: string }; result: void };
  /** 写系统剪贴板；renderer 的 navigator.clipboard 失败时的兜底。 */
  'host.copyText': { params: { text: string }; result: boolean };
  /** Dock/任务栏角标（待审批数）。Windows 需 overlay icon，一期不做，静默忽略。 */
  'host.setBadgeCount': { params: { count: number }; result: void };
}

export type MethodName = keyof Methods;
export type ParamsOf<M extends MethodName> = Methods[M]['params'];
export type ResultOf<M extends MethodName> = Methods[M]['result'];

/** 单个 RPC 请求的载荷（走 IPC_CHANNEL_RPC）。 */
export interface RpcRequest {
  method: MethodName;
  params: unknown;
}

/**
 * 主进程推给 renderer 的事件。一期只有 badge 相关的空集，但通道与
 * 联合类型的形状现在就定死：第二期的长任务进度（job.progress / job.log /
 * job.done / job.error）直接往这里加成员，preload 与前端订阅代码不用改。
 */
export type HostEvent =
  | { type: 'host.serverChanged'; serverUrl: string }
  /** 原生菜单「服务器…」→ 让前端打开配置页（换服务器的唯一入口，一期不做设置页分组）。 */
  | { type: 'host.openServerSetup' };

export const IPC_CHANNEL_RPC = 'polaris:rpc';
export const IPC_CHANNEL_EVENT = 'polaris:event';
export const IPC_CHANNEL_INFO_SYNC = 'polaris:info-sync';

/** 结构化错误码：renderer 据此区分「能力不可用」与真实故障。 */
export const ERR_UNKNOWN_METHOD = 'ERR_UNKNOWN_METHOD';
export const ERR_INVALID_PARAMS = 'ERR_INVALID_PARAMS';
export const ERR_CAPABILITY_UNAVAILABLE = 'ERR_CAPABILITY_UNAVAILABLE';
