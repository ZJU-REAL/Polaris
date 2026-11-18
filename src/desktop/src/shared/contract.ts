/* ============================================================
   Main ↔ renderer 的唯一契约。

   刻意做成「单通道 + 方法表」而不是每个能力一个 ipcMain.handle：
   preload 是打进安装包、renderer 直接可见的边界，一旦按能力开通道，
   将来每加一个本地能力都要同时改 preload + main + renderer 三处。
   单通道让 preload 写完就不再改——加方法只动本文件与 main 侧实现。

   方法命名：<域>.<对象>.<动词>。
   host.*   = 外壳能力，桌面端恒可用。
   local.*  = 本地长任务簿记（目前只有 job.cancel）。真正的本地计算走
              kernel 里的插件（legacy-engine 等），不再经过独立 agent 进程。
   kernel.* = 内核（@polaris/kernel）状态，桌面端恒可用。
   ============================================================ */

/** preload 同步注入到 window.__POLARIS__ 的静态事实（见 preload/index.ts 的说明）。 */
export interface HostInfo {
  platform: 'darwin' | 'win32' | 'linux';
  appVersion: string;
}

/**
 * 契约版本。前端读到比自己新的 host 时按能力表降级，而不是按版本号写特判。
 *
 * 2（#705 起）：方法表换过一轮——local.* 去掉，kernel.status 与 plugins.* 进来。
 * 界面热更新走的就是这个号：CI 把它写进包名（renderer-<版本>-c<契约>.tar.gz），
 * 老外壳看到比自己大的号就不收这份界面，改走安装器。这一步不能省——插件页按能力
 * 表自动隐藏，老外壳装上新界面**不会报错**，只会悄悄少一块功能，然后因为版本号
 * 已经追平而不再提示更新，用户就永远停在收不到插件的外壳上了。
 *
 * 3（#842 起）：去掉远程服务器模式——host.setServerUrl / host.testServer 与
 * 「换服务器」事件删除，加 host.relaunch。新界面只认本机引擎，装到还可能连着
 * 服务器的老外壳上会把用户卡在「引擎没启动」页，所以老外壳必须走安装器。
 *
 * 4：加 host.diagnostics / host.openLogsFolder / host.network.*，删掉没人调的
 * host.info；界面开始依赖引擎状态事件（engine.state）在引擎崩溃后自己恢复。
 * 界面包的文件名同时带上了后端哈希（renderer-<版本>-c<契约>-b<哈希>.tar.gz）：
 * 契约号只说明桥接层没变，说明不了 Python 引擎没变——只有引擎也一样时才能只换界面。
 */
export const CONTRACT_VERSION = 4;

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

/** 插件管理（#705）：设置页插件 tab 读它显隐。目前整体关闭，见 capabilities.ts 的
    PLUGINS_UI_ENABLED。 */
export const CAPABILITY_PLUGINS_MANAGE = 'plugins.manage';

/** 长任务句柄：invoke 立刻返回它，进度经事件通道推。 */
export interface JobHandle {
  jobId: string;
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
  /**
   * 为什么是 full：engine = 新版本的本机引擎与已装的不同（只换界面会让新界面跑在
   * 旧引擎上）；shell = 外壳本身要更新（契约升级）；no-renderer = 这个版本没发界面包。
   */
  fullReason?: 'engine' | 'shell' | 'no-renderer';
  /** 该更新要求的 IPC 契约版本。 */
  contract?: number;
  downloadUrl?: string;
  downloadSize?: number;
  /** 整包安装器地址；热更新装不上时退回它。 */
  installerUrl?: string;
  /**
   * 这次检查确实查到了 GitHub 并比过了版本。只有它为 true，界面才敢说「已是最新」
   * （#812）——查失败和没有新版在 available 上长得一样。老外壳不带这个字段。
   */
  checked?: boolean;
  /**
   * 没查成的原因代码：rate-limited / network / http / no-package。文案由界面配，
   * 主进程不知道界面是什么语言。
   */
  error?: string;
  /** 原因的细节（如 HTTP 状态码），给界面附在文案后面。 */
  errorDetail?: string;
  /** 这个版本的发布页，给「查看发布页」用。 */
  releaseUrl?: string;
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
 * 本地引擎地址。内核没拉起本地后端（开发态未设 POLARIS_DESKTOP_ENGINE、
 * 或引擎启动失败）时 baseUrl 为 null，前端据此显示「引擎没启动」页。
 */
export interface LocalBackendInfo {
  baseUrl: string | null;
  /**
   * 本次启动的本地会话口令（#850）：界面取会话时放进 X-Polaris-Session-Secret 头，
   * 引擎只认带着它的 /auth/local-session 请求。老外壳不带这个字段（它的引擎也不要求）。
   */
  sessionSecret?: string | null;
  /**
   * 引擎没起来的已知原因（#850），给兜底页说清楚：
   * port-in-use = 端口被别的程序占着；stale-engine = 还有一个旧的 Polaris 引擎在跑。
   */
  problem?: 'port-in-use' | 'stale-engine' | null;
  /**
   * 引擎进程的运行状态：running = 在跑；restarting = 中途退出了，正在自动重启；
   * failed = 重启也没成；none = 这次没有引擎（没装、没起来）。baseUrl 只在 running 时非空。
   */
  state?: EngineRunState;
}

export type EngineRunState = 'running' | 'restarting' | 'failed' | 'none';

/**
 * 引擎没准备好/没起来/中途挂掉的原因，给兜底页显示真实的错误而不是一句「检查网络」。
 * code：disk-space = 磁盘空间不足；download = 下载 Python 或依赖失败；
 * install = 环境装不起来；resources = 安装包不完整；engine-start = 环境装好了但引擎
 * 没起来；engine-crashed = 运行中退出且自动重启没成；tree = 内部配置不可用。
 * lines：最后几行相关输出（已抹掉密钥）。
 */
export interface EngineFailure {
  code: 'disk-space' | 'download' | 'install' | 'resources' | 'engine-start' | 'engine-crashed' | 'tree';
  message: string;
  lines: string[];
  at: number;
}

/** 联网设置（首启安装用的代理与镜像策略）。 */
export interface NetworkSettings {
  /** 代理地址；空串 = 不指定（沿用环境变量 / 系统代理）。 */
  proxy: string;
  /** auto = 官方源不通再换国内镜像；on = 先用镜像；off = 不用镜像。 */
  mirror: 'auto' | 'on' | 'off';
}

/** 「复制诊断信息」与兜底页用的一份快照。text 是拼好的纯文本，可直接贴进 issue。 */
export interface Diagnostics {
  /** 外壳（安装包）版本。 */
  appVersion: string;
  /** 眼前这份界面的版本；热更新过才会与 appVersion 不同。 */
  uiVersion: string;
  /** 引擎自报的版本；引擎没在跑时为 null。 */
  engineVersion: string | null;
  os: string;
  engineState: EngineRunState;
  /** 首启安装/引擎启动所处的阶段（EngineBootstrapStatus.phase）。 */
  bootstrapPhase: string;
  failure: EngineFailure | null;
  /** 新环境没装成、暂时沿用旧环境时的原因。 */
  degraded: string | null;
  logsDir: string;
  dataDir: string;
  text: string;
}

/**
 * 内嵌引擎（打包态自带的 Python 后端）的引导进度。首启要下载 Python
 * 工具链并安装依赖，可能长达数分钟——窗口先于内核创建（#721），前端的
 * 首启等待页轮询这个方法。
 * phase：starting（内核启动中，路径未定）/ check / python / venv / install /
 * engine（环境已装好，引擎进程启动中）/ ready / idle（没走内嵌路径：开发态
 * 或显式 env）/ failed（引导或引擎启动失败）。
 * done 在 ready / idle / failed 时为 true。
 *
 * 其余字段可选、只在走内嵌引导时出现（老渲染层忽略即可，不必升契约版本），
 * 让等待页「看得出没卡住」：
 * - phaseStartedAt：当前阶段开始的时刻（epoch ms）
 * - log：当前（或最近一个有输出的）阶段最后几行 uv 输出，已去 ANSI、截断
 * - lastOutputAt：最近一行输出的时刻（epoch ms）
 * - downloadedBytes：python / install 阶段 engine 目录相对阶段开始的落盘增长
 */
export interface EngineBootstrapStatus {
  phase: string;
  done: boolean;
  phaseStartedAt?: number;
  log?: string[];
  lastOutputAt?: number;
  downloadedBytes?: number;
  /** 引擎运行中退出、正在自动重启（phase 此时是 engine）。等待页据此换一句说明。 */
  restarting?: boolean;
  /** phase=failed 时的原因与最后几行输出。 */
  failure?: EngineFailure;
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

export interface Methods {
  /** 退出并重新打开应用（本机引擎没起来时的「重试」）。 */
  'host.relaunch': { params: void; result: void };
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
  /** 版本、系统、引擎状态与最近的引擎日志，拼成可直接复制的文本。 */
  'host.diagnostics': { params: void; result: Diagnostics };
  /** 在文件管理器里打开日志文件夹；打不开返回 false。 */
  'host.openLogsFolder': { params: void; result: boolean };
  /** 读联网设置（代理、镜像策略）。 */
  'host.network.get': { params: void; result: NetworkSettings };
  /** 改联网设置；下次安装（重新打开应用）时生效。代理地址不合法抛 ERR_INVALID_PARAMS。 */
  'host.network.set': { params: { proxy: string; mirror: string }; result: NetworkSettings };

  /** 内核状态探针。界面不依赖它做分支；壳级 E2E 用它判断内核就绪、数插件数。 */
  'kernel.status': { params: void; result: KernelStatus };
  /** 本地引擎地址；前端启动时问一次，非空则 REST/WS 全走本地。 */
  'kernel.localBackend': { params: void; result: LocalBackendInfo };
  /** 内嵌引擎引导进度；首启等待页轮询它决定何时放行进应用。 */
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

  /* ---- local.*：本地长任务簿记。曾经还声明过 latex.compile / fs.pickFolder /
     papers.scan 三个只会抛 ERR_CAPABILITY_UNAVAILABLE 的占位方法（走独立
     stdio agent 进程），审计证明那条管道从未产生过一次成功调用，连同 agent
     一起拆除（#731）；本地计算的落点改为 kernel 插件。 ---- */

  /** 取消长任务（更新下载等 job.* 事件流的取消口）。 */
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
  /* ---- 长任务事件。形状现在定死：第二期的编译日志与扫描进度直接用它们，
     preload 与前端订阅代码不需要再改。 ---- */
  | { type: 'job.progress'; jobId: string; phase: string; done: number; total: number; note?: string }
  | { type: 'job.log'; jobId: string; chunk: string }
  | { type: 'job.done'; jobId: string; result: unknown }
  | { type: 'job.error'; jobId: string; code: string; message: string }
  /* 引擎运行状态变了：restarting = 中途退出、正在自动重启；running = 回来了；
     failed = 没救回来。界面收到后换到等待页/兜底页，回来后自己重新加载。 */
  | { type: 'engine.state'; state: 'running' | 'restarting' | 'failed'; reason?: string };

export const IPC_CHANNEL_RPC = 'polaris:rpc';
export const IPC_CHANNEL_EVENT = 'polaris:event';
export const IPC_CHANNEL_INFO_SYNC = 'polaris:info-sync';

/** 结构化错误码：renderer 据此区分「能力不可用」与真实故障。 */
export const ERR_UNKNOWN_METHOD = 'ERR_UNKNOWN_METHOD';
export const ERR_INVALID_PARAMS = 'ERR_INVALID_PARAMS';
export const ERR_CAPABILITY_UNAVAILABLE = 'ERR_CAPABILITY_UNAVAILABLE';
