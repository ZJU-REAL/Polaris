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

/** 契约版本。前端读到比自己新的 host 时按能力表降级，而不是按版本号写特判。 */
export const CONTRACT_VERSION = 1;

/** 单个能力的可用性。detail 给前端做提示（如 tectonic 装了但缓存是空的）。 */
export interface CapabilityState {
  available: boolean;
  /** 不可用的原因，仅用于展示与排错，不要拿来做分支判断。 */
  reason?: string;
  detail?: unknown;
}

/**
 * 能力清单。前端所有「要不要走本地」的判断只读这张表——
 * 绝不读 platform、绝不读版本号做特判，否则第二期增删能力又要改前端。
 */
export interface CapabilityManifest {
  hostVersion: string;
  platform: HostInfo['platform'];
  contract: number;
  capabilities: Record<string, CapabilityState>;
}

/** 已知能力键。第二期实现时把对应的 available 翻成 true。 */
export const CAPABILITY_LATEX_COMPILE = 'latex.compile';
export const CAPABILITY_PAPER_IMPORT = 'papers.import';
export const CAPABILITY_PDF_CACHE = 'cache.pdf';

/** 长任务句柄：invoke 立刻返回它，进度经事件通道推。 */
export interface JobHandle {
  jobId: string;
}

export type FolderPurpose = 'paper-import' | 'zotero-library';

export interface LatexCompileInput {
  manuscriptId: string;
  engine: 'tectonic' | 'pdflatex' | 'xelatex' | 'lualatex';
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
  'host.capabilities': { params: void; result: CapabilityManifest };

  /* ---- local.*：第二期的本地计算能力 ----
     现在全部声明但不实现（一律抛 ERR_CAPABILITY_UNAVAILABLE），目的是把
     renderer → preload → router → agent supervisor 这条管道**现在就打通**。
     等第二期真的接上本地进程时，改的只有 agent 侧的 handler。 */

  /** 本地 tectonic 编译。返回 JobHandle，日志与结果经 job.* 事件推。 */
  'local.latex.compile': { params: LatexCompileInput; result: JobHandle };
  /** 弹原生目录选择框，返回不透明句柄 token（绝不把真实路径交给 renderer）。 */
  'local.fs.pickFolder': { params: { purpose: FolderPurpose }; result: { token: string } | null };
  /** 扫描已授权目录里的 PDF（识别 arXiv id / DOI）。 */
  'local.papers.scan': { params: { token: string }; result: JobHandle };
  /** 取消长任务。 */
  'local.job.cancel': { params: { jobId: string }; result: void };
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
  | { type: 'host.openServerSetup' }
  /* ---- 长任务事件。形状现在定死：第二期的编译日志与扫描进度直接用它们，
     preload 与前端订阅代码不需要再改。 ---- */
  | { type: 'job.progress'; jobId: string; phase: string; done: number; total: number; note?: string }
  | { type: 'job.log'; jobId: string; chunk: string }
  | { type: 'job.done'; jobId: string; result: unknown }
  | { type: 'job.error'; jobId: string; code: string; message: string };

export const IPC_CHANNEL_RPC = 'polaris:rpc';
export const IPC_CHANNEL_EVENT = 'polaris:event';
export const IPC_CHANNEL_INFO_SYNC = 'polaris:info-sync';

/** 结构化错误码：renderer 据此区分「能力不可用」与真实故障。 */
export const ERR_UNKNOWN_METHOD = 'ERR_UNKNOWN_METHOD';
export const ERR_INVALID_PARAMS = 'ERR_INVALID_PARAMS';
export const ERR_CAPABILITY_UNAVAILABLE = 'ERR_CAPABILITY_UNAVAILABLE';
