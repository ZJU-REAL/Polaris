/* ============================================================
   桌面宿主桥 —— 前端访问 window.polaris 的唯一封装。

   业务代码永远不要直接读 window.polaris：web 端它不存在，直接读会让
   web 构建到处需要判空。这里统一收口，web 端所有调用都是安全的 no-op。

   类型是 src/desktop/src/shared/contract.ts 的镜像，而不是 import 过来的：
   docker/Dockerfile.frontend 只 COPY src/frontend，跨目录 import 会让前端
   镜像构建直接失败。契约以 desktop 侧那份为准，改动需两边同步。
   ============================================================ */

export type ServerProbe =
  | { ok: true; version: string }
  | { ok: false; reason: 'invalid-url' | 'unreachable' | 'timeout' | 'not-polaris'; detail?: string };

export type HostEvent =
  | { type: 'host.serverChanged'; serverUrl: string }
  | { type: 'host.openServerSetup' }
  | { type: 'job.progress'; jobId: string; phase: string; done: number; total: number; note?: string }
  | { type: 'job.log'; jobId: string; chunk: string }
  | { type: 'job.done'; jobId: string; result: unknown }
  | { type: 'job.error'; jobId: string; code: string; message: string };

export interface CapabilityState {
  available: boolean;
  reason?: string;
  detail?: unknown;
}

export interface CapabilityManifest {
  hostVersion: string;
  platform: string;
  contract: number;
  capabilities: Record<string, CapabilityState>;
}

interface HostBridge {
  invoke(method: string, params?: unknown): Promise<unknown>;
  subscribe(handler: (event: HostEvent) => void): number;
  unsubscribe(id: number): void;
}

function bridge(): HostBridge | undefined {
  return typeof window === 'undefined'
    ? undefined
    : (window as unknown as { polaris?: HostBridge }).polaris;
}

/** 桌面端平台标识；web 端返回 null。 */
export function hostPlatform(): 'darwin' | 'win32' | 'linux' | null {
  const info = typeof window === 'undefined' ? undefined : window.__POLARIS__;
  return info?.platform ?? null;
}

/** 桌面客户端版本号；web 端返回 null。 */
export function hostAppVersion(): string | null {
  const info = typeof window === 'undefined' ? undefined : window.__POLARIS__;
  return info?.appVersion ?? null;
}

/** 是否有可用的桌面宿主桥（比 endpoint.isDesktop() 更严格：桥必须真的注入成功）。 */
export function hasHost(): boolean {
  return bridge() != null;
}

/** 探测服务器地址是否可用；web 端返回 null（该功能只在桌面端有意义）。 */
export async function testServer(url: string): Promise<ServerProbe | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('host.testServer', { url })) as ServerProbe;
}

/** 保存服务器地址；主进程会随即重建窗口，本调用之后的代码不保证还会执行。 */
export async function setServerUrl(url: string): Promise<void> {
  const b = bridge();
  if (!b) return;
  await b.invoke('host.setServerUrl', { url });
}

/** 用系统浏览器打开外链。 */
export async function openExternal(url: string): Promise<void> {
  await bridge()?.invoke('host.openExternal', { url });
}

/** 兜底剪贴板写入（navigator.clipboard 失败时用）。 */
export async function hostCopyText(text: string): Promise<boolean> {
  const b = bridge();
  if (!b) return false;
  return (await b.invoke('host.copyText', { text })) === true;
}

/** Dock/任务栏角标（待审批数）。 */
export function setBadgeCount(count: number): void {
  void bridge()?.invoke('host.setBadgeCount', { count });
}

/** 本地引擎信息（contract.ts 的 LocalBackendInfo 镜像）。 */
export interface LocalBackendInfo {
  baseUrl: string | null;
}

/**
 * 问主进程要本地引擎地址；web 端返回 null（无桥，零请求）。
 * 只被 endpoint.ts 的启动探测与首启等待页调用，业务代码不要直接用。
 */
export async function kernelLocalBackend(): Promise<LocalBackendInfo | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('kernel.localBackend')) as LocalBackendInfo;
}

/** 内嵌引擎引导进度（contract.ts 的 EngineBootstrapStatus 镜像）。 */
export interface EngineBootstrapStatus {
  phase: string;
  done: boolean;
}

/**
 * 内嵌引擎引导进度；web 端返回 null。窗口先于内核创建（#721），首启
 * 等待页靠轮询它决定何时放行进应用。invoke 失败（旧宿主/桥故障）也折叠
 * 成 null——按「无引导流程」处理，宁可回落远端也不卡死首屏。
 */
export async function engineBootstrapStatus(): Promise<EngineBootstrapStatus | null> {
  const b = bridge();
  if (!b) return null;
  try {
    return (await b.invoke('kernel.engineBootstrapStatus')) as EngineBootstrapStatus;
  } catch {
    return null;
  }
}

/* —— 能力清单 ——
   前端所有走本地还是走远端的判断只读这张表：绝不读 platform、绝不读版本号
   做特判，否则第二期能力增删又要回来改前端。 */

let manifest: CapabilityManifest | null = null;

/** 拉一次能力清单并缓存；web 端返回 null。 */
export async function loadCapabilities(): Promise<CapabilityManifest | null> {
  const b = bridge();
  if (!b) return null;
  manifest = (await b.invoke('host.capabilities')) as CapabilityManifest;
  return manifest;
}

/** 同步查询某项能力。清单还没拉到时一律当作不可用（宁可走服务器）。 */
export function isCapabilityAvailable(capability: string): boolean {
  return manifest?.capabilities[capability]?.available === true;
}

export function capabilitySnapshot(): CapabilityManifest | null {
  return manifest;
}

/** 供 invoke 的通用出口（host-jobs 等内部模块用）。 */
export async function invokeHost(method: string, params?: unknown): Promise<unknown> {
  const b = bridge();
  if (!b) throw new Error('desktop host unavailable');
  return await b.invoke(method, params);
}

/* —— 应用更新 —— */

export interface UpdateInfo {
  available: boolean;
  currentVersion: string;
  latestVersion?: string;
  notes?: string;
  publishedAt?: string;
  /** hot=换界面即可、免重启；full=要装安装器。 */
  kind?: 'hot' | 'full';
  contract?: number;
  downloadUrl?: string;
  downloadSize?: number;
}

/** 查有没有新版本；web 端返回 null。主进程失败时返回 available:false，不抛错。 */
export async function checkUpdate(): Promise<UpdateInfo | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('host.update.check')) as UpdateInfo;
}

/** 下载并应用更新，返回 JobHandle；进度经 job.* 事件推送（用 invokeJob 更方便）。 */
export async function applyUpdate(): Promise<{ jobId: string } | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('host.update.apply')) as { jobId: string };
}

/** 订阅宿主事件，返回取消订阅函数（web 端返回 no-op）。 */
export function onHostEvent(handler: (event: HostEvent) => void): () => void {
  const b = bridge();
  if (!b) return () => {};
  const id = b.subscribe(handler);
  return () => b.unsubscribe(id);
}

/* —— 插件管理（plugins.*，#705；contract.ts 的手工镜像）——
   仅桌面端有意义：设置页插件 tab 的显隐读 plugins.manage 能力位，
   web 端（无桥）所有调用都是安全的 null/no-op、零请求。 */

/** 插件管理能力键（isCapabilityAvailable 用）。 */
export const CAPABILITY_PLUGINS_MANAGE = 'plugins.manage';

/** 单个插件条目的运行时视图。disabled 是持久开关（用户意图），state 是运行事实。 */
export interface PluginEntryInfo {
  id: string;
  name: string;
  disabled: boolean;
  state: 'active' | 'disabled' | 'error' | 'pending';
  error?: string;
  config?: unknown;
}

export interface PluginValidationError {
  path: string;
  message: string;
}

/** 配置校验结果：错误是数据（就地回显），装载失败才是异常。 */
export interface PluginValidationResult {
  ok: boolean;
  errors: PluginValidationError[];
}

export interface PluginTreeEntry {
  id: string;
  name: string;
  config?: unknown;
  disabled?: boolean;
  children?: PluginTreeEntry[];
}

/** 整树导出/导入载荷；version 不为 1 的载荷主进程直接拒绝。 */
export interface PluginTreeExport {
  version: 1;
  entries: PluginTreeEntry[];
}

/** 全部插件条目 + 运行态；web 端返回 null。 */
export async function listPlugins(): Promise<PluginEntryInfo[] | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.list')) as PluginEntryInfo[];
}

/** 启用插件，返回变更后的条目；启动失败时抛错（主进程侧树已回滚）。 */
export async function enablePlugin(id: string): Promise<PluginEntryInfo | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.enable', { id })) as PluginEntryInfo;
}

/** 禁用插件（fiber 级联回收），返回变更后的条目。 */
export async function disablePlugin(id: string): Promise<PluginEntryInfo | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.disable', { id })) as PluginEntryInfo;
}

/** 校验并应用配置：ok=false 表示 schema 未过、什么都没改。 */
export async function updatePluginConfig(
  id: string,
  config: Record<string, unknown>,
): Promise<PluginValidationResult | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.updateConfig', { id, config })) as PluginValidationResult;
}

/** 只校验不应用（配置编辑器实时回显）。 */
export async function validatePluginConfig(
  name: string,
  config: Record<string, unknown>,
): Promise<PluginValidationResult | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.validateConfig', { name, config })) as PluginValidationResult;
}

/** 导出整棵配置树（备份/迁移）；web 端返回 null。 */
export async function exportPluginTree(): Promise<PluginTreeExport | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.exportTree')) as PluginTreeExport;
}

/** 全量替换整树。主进程导入前自动留 last-good 快照、失败回滚。 */
export async function importPluginTree(tree: PluginTreeExport): Promise<void> {
  await bridge()?.invoke('plugins.importTree', { tree });
}

/* —— 插件市场（plugins.market.*，#708；contract.ts 的手工镜像）——
   显隐同样读 plugins.manage 能力位；web 端（无桥）一律 null/no-op。 */

/** 市场索引单条目（desktop contract.ts 的 MarketIndexEntry 镜像）。 */
export interface MarketIndexEntry {
  name: string;
  version: string;
  kind: 'datasource' | 'record-kind' | 'runner' | 'agent-tool' | 'workflow' | 'discipline' | 'panel';
  description: string;
  publisher: string;
  /** 权限摘要：如实展示，v1 不 enforcement。 */
  permissions: { network?: boolean; filesystem?: boolean };
  tier: 'bronze' | 'silver' | 'gold' | 'platinum';
  badges: string[];
}

/** 当前市场索引源；isDefault 供 UI 显示「官方源/自定义源」。 */
export interface MarketEndpoint {
  endpoint: string;
  isDefault: boolean;
}

/** 卸载结果是数据：enabled 拒卸提示用户先禁用，不当异常抛。 */
export type MarketUninstallResult =
  | { ok: true }
  | { ok: false; code: 'plugin-enabled' | 'not-installed'; message: string };

/** 拉取市场索引（源地址是主进程的持久配置）；web 端返回 null。 */
export async function fetchMarketIndex(): Promise<MarketIndexEntry[] | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.market.fetchIndex')) as MarketIndexEntry[];
}

/** 安装插件：返回 JobHandle，下载/校验/解压/登记进度走 job.* 事件。
    装/启分离：装完是 disabled 条目，用户在插件列表里显式启用。 */
export async function installMarketPlugin(
  name: string,
  version: string,
): Promise<{ jobId: string } | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.market.install', { name, version })) as { jobId: string };
}

/** 卸载插件；条目仍启用时返回 ok:false（先禁用再卸）。
    name 收 npm 包名或树条目 id 皆可（kernel 侧双解析）——前端列表里
    可靠可得的只有条目 id，传 id 即可。 */
export async function uninstallMarketPlugin(name: string): Promise<MarketUninstallResult | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.market.uninstall', { name })) as MarketUninstallResult;
}

/** 当前索引源；web 端返回 null。 */
export async function getMarketEndpoint(): Promise<MarketEndpoint | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.market.getEndpoint')) as MarketEndpoint;
}

/** 设置索引源；空串复位官方默认源。 */
export async function setMarketEndpoint(endpoint: string): Promise<MarketEndpoint | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('plugins.market.setEndpoint', { endpoint })) as MarketEndpoint;
}
