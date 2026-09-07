/* ============================================================
   Main ↔ renderer 的唯一契约。

   刻意做成「单通道 + 方法表」而不是每个能力一个 ipcMain.handle：
   preload 是打进安装包、renderer 直接可见的边界，一旦按能力开通道，
   将来每加一个本地能力都要同时改 preload + main + renderer 三处。
   单通道让 preload 写完就不再改——加方法只动本文件与 main 侧实现。

   方法命名：<域>.<对象>.<动词>。
   host.*   = 外壳能力，桌面端恒可用。
   local.*  = 本地计算能力（第二期），届时只往 Methods 里加条目。
   kernel.* = 内核（@polaris/kernel）状态，桌面端恒可用。
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
/** 插件管理（#705）：kernel 活着且配置树服务可达时可用，设置页插件 tab 读它显隐。 */
export const CAPABILITY_PLUGINS_MANAGE = 'plugins.manage';

/** 长任务句柄：invoke 立刻返回它，进度经事件通道推。 */
export interface JobHandle {
  jobId: string;
}

export type FolderPurpose = 'paper-import' | 'zotero-library';

export interface LatexCompileInput {
  manuscriptId: string;
  engine: 'tectonic' | 'pdflatex' | 'xelatex' | 'lualatex';
}

/** 更新检查结果。available=false 时其余字段无意义。 */
export interface UpdateInfo {
  available: boolean;
  currentVersion: string;
  latestVersion?: string;
  /** release 正文，直接当 markdown 渲染。 */
  notes?: string;
  publishedAt?: string;
  /** hot=换界面即可、免重启；full=要装安装器。 */
  kind?: 'hot' | 'full';
  /** 该更新要求的 IPC 契约版本。 */
  contract?: number;
  downloadUrl?: string;
  downloadSize?: number;
  /** 整包安装器地址；热更新装不上时退回它。 */
  installerUrl?: string;
}

/**
 * 内核运行状态。plugins = cordis registry 里已注册的插件 runtime 数量
 * （ctx.registry.size，公开口径）。树驱动装载（#703）后名额包括直挂的
 * storage、Loader（连带其内部 isolate）、SqliteTree，以及配置树条目拉起的
 * 内置插件（desktop-probe / sources 等），恒 ≥ 5 即为健康。
 */
export interface KernelStatus {
  started: boolean;
  name: string;
  plugins: number;
  /** SQLite 持久层（#609）是否就绪。false = 本次会话配置树不落盘。 */
  storage: boolean;
}

/**
 * 本地引擎地址。内核没拉起本地后端（未设 POLARIS_DESKTOP_ENGINE、或引擎
 * 启动失败）时 baseUrl 为 null，前端据此回落远端服务器。
 */
export interface LocalBackendInfo {
  baseUrl: string | null;
}

/**
 * 内嵌引擎（打包态自带的 Python 后端）的引导进度。首启要下载 Python
 * 工具链并安装依赖，可能长达数分钟——前端进度条二期接这个方法轮询。
 * phase：idle（没走内嵌路径）/ check / python / venv / install / ready /
 * failed（引导失败，已回落远端流程）。done 在 ready 或 failed 时为 true。
 */
export interface EngineBootstrapStatus {
  phase: string;
  done: boolean;
}

/* ---- plugins.*（#705）：配置树管理的载荷类型 ---- */

/**
 * 单个插件条目的运行时视图（kernel 侧 SqliteTree.listEntries 的镜像）。
 * disabled 是树上的持久开关（用户意图），state 才是运行事实：
 * active=fiber 已装载；disabled=按开关未实例化；error=fiber 启动/运行失败；
 * pending=在等依赖服务（或树刚建还没跑完）。
 */
export interface PluginEntryInfo {
  id: string;
  /** 装载 specifier，内置插件形如 cordis:sources。 */
  name: string;
  disabled: boolean;
  state: 'active' | 'disabled' | 'error' | 'pending';
  /** state='error' 时是 fiber 记录的失败原因；disabled 条目上也可能携带
      诊断注记（#708：安装物哈希不符被强制禁用的解释）。 */
  error?: string;
  /** 条目当前配置。组条目（children 容器）不暴露 config。 */
  config?: unknown;
}

/** schemastery 校验结果：错误作为数据返回而不是抛异常，前端就地回显。 */
export interface PluginValidationError {
  /** 出错字段路径；解析不出来时为空串，message 里带完整描述。 */
  path: string;
  message: string;
}

export interface PluginValidationResult {
  ok: boolean;
  errors: PluginValidationError[];
}

/** 整树导出条目（kernel ConfigEntry 的镜像形状）。 */
export interface PluginTreeEntry {
  id: string;
  name: string;
  config?: unknown;
  disabled?: boolean;
  children?: PluginTreeEntry[];
}

/** 整树导出/导入载荷。version 定死为 1，导入时不认识的版本直接拒绝。 */
export interface PluginTreeExport {
  version: 1;
  entries: PluginTreeEntry[];
}

/* ---- plugins.market.*（#708）：官方插件市场的载荷类型 ---- */

/** 官方市场索引源的默认地址（主仓 market/index.json 的 raw URL）。 */
export const MARKET_ENDPOINT_DEFAULT =
  'https://raw.githubusercontent.com/ZJU-REAL/Polaris/main/market/index.json';

/** 市场索引单条目（kernel 侧 MarketIndexEntry 的镜像形状）。 */
export interface MarketIndexEntry {
  /** npm 包名（polaris-plugin-* / @scope/polaris-plugin-*）。 */
  name: string;
  /** 上架版本；安装时按此精确版本向 registry 解析。 */
  version: string;
  kind: 'datasource' | 'record-kind' | 'runner' | 'agent-tool' | 'workflow' | 'discipline' | 'panel';
  description: string;
  publisher: string;
  /** 权限摘要：如实展示，v1 不 enforcement。 */
  permissions: { network?: boolean; filesystem?: boolean };
  tier: 'bronze' | 'silver' | 'gold' | 'platinum';
  /** 治理徽章（official/verified/preview…），开放枚举。 */
  badges: string[];
}

/** 当前市场索引源。isDefault 供 UI 显示「官方源/自定义源」。 */
export interface MarketEndpoint {
  endpoint: string;
  isDefault: boolean;
}

/** 卸载结果作为数据返回：enabled 拒卸是业务分支，不是异常。 */
export type MarketUninstallResult =
  | { ok: true }
  | { ok: false; code: 'plugin-enabled' | 'not-installed'; message: string };

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
  /** 查有没有新版本。失败一律当作「没有更新」，不打扰用户。 */
  'host.update.check': { params: void; result: UpdateInfo };
  /** 下载并应用上一次检查到的更新；进度走 job.* 事件。 */
  'host.update.apply': { params: void; result: JobHandle };

  /** 内核状态探针。前端不依赖它做分支，只用于诊断页与冒烟。 */
  'kernel.status': { params: void; result: KernelStatus };
  /** 本地引擎地址；前端启动时问一次，非空则 REST/WS 全走本地。 */
  'kernel.localBackend': { params: void; result: LocalBackendInfo };
  /** 内嵌引擎引导进度；首启 bootstrap 期间轮询可得阶段信息。 */
  'kernel.engineBootstrapStatus': { params: void; result: EngineBootstrapStatus };

  /* ---- plugins.*：配置树管理（#705）。能力位 plugins.manage 不可用
     （kernel 没起来 / 配置树挂载失败）时全族抛 ERR_CAPABILITY_UNAVAILABLE。
     校验分两层：router 只做 IPC 形状（字符串/对象/递归树形），语义校验
     （__jsExpr、未知字段、重复 id、schema）在 kernel 进程内完成。 ---- */

  /** 全部条目 + 运行态。 */
  'plugins.list': { params: void; result: PluginEntryInfo[] };
  /** 启用条目（删掉树上的 disabled 键并拉起 fiber）；失败抛错且树回滚。 */
  'plugins.enable': { params: { id: string }; result: PluginEntryInfo };
  /** 禁用条目（fiber 级联 dispose，树上留 disabled 占位）。 */
  'plugins.disable': { params: { id: string }; result: PluginEntryInfo };
  /** 校验并应用配置。ok=false 表示 schema 未过、什么都没改。 */
  'plugins.updateConfig': { params: { id: string; config: unknown }; result: PluginValidationResult };
  /** 只校验不应用（配置编辑器的实时回显）。 */
  'plugins.validateConfig': { params: { name: string; config: unknown }; result: PluginValidationResult };
  /** 导出当前整树（备份/迁移）。 */
  'plugins.exportTree': { params: void; result: PluginTreeExport };
  /** 全量替换整树。导入前 kernel 自动留 last-good 快照，失败回滚。 */
  'plugins.importTree': { params: { tree: PluginTreeExport }; result: void };

  /* ---- plugins.market.*（#708）：官方源市场。能力门槛同上（plugins.manage），
     且 install/uninstall 还要求持久层在位（安装记录必须落库，否则哈希
     复核链路断裂）。 ---- */

  /** 拉取并校验市场索引（源地址读 getEndpoint 的持久配置）。 */
  'plugins.market.fetchIndex': { params: void; result: MarketIndexEntry[] };
  /** 安装插件：返回 JobHandle，下载/校验/解压/登记四个进度点走 job.* 事件。
      装/启分离：装完只在树上挂 disabled 条目，代码一行不执行。 */
  'plugins.market.install': { params: { name: string; version: string }; result: JobHandle };
  /** 卸载插件。树条目仍启用时拒绝（错误作为数据返回，先禁用再卸）。 */
  'plugins.market.uninstall': { params: { name: string }; result: MarketUninstallResult };
  /** 当前索引源。 */
  'plugins.market.getEndpoint': { params: void; result: MarketEndpoint };
  /** 设置索引源；空串 = 复位官方默认源。 */
  'plugins.market.setEndpoint': { params: { endpoint: string }; result: MarketEndpoint };

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
