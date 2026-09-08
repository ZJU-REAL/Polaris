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
  type PluginTreeExport,
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

/**
 * 插件配置载荷：一律是纯对象。数组/原始值/null 在 schema 层面没有合法
 * 形状，在 IPC 边界先挡掉；对象内部的语义（__jsExpr、schema 匹配）归
 * kernel 层校验——两层各管一层，见 methods.plugins.ts 文件头。
 */
function asPluginConfig(params: unknown): Record<string, unknown> {
  const value = (params as Record<string, unknown> | null)?.['config'];
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${ERR_INVALID_PARAMS}: config must be a plain object`);
  }
  return value as Record<string, unknown>;
}

/** 树导入嵌套上限。真实树两三层，16 层只为拦住构造出来的深递归载荷。 */
const MAX_TREE_DEPTH = 16;

function assertTreeEntryShape(value: unknown, path: string, depth: number): void {
  if (depth > MAX_TREE_DEPTH) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path} exceeds max tree depth ${MAX_TREE_DEPTH}`);
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path} must be an object`);
  }
  const entry = value as Record<string, unknown>;
  if (typeof entry.id !== 'string' || !entry.id) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path}.id must be a non-empty string`);
  }
  if (typeof entry.name !== 'string' || !entry.name) {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path}.name must be a non-empty string`);
  }
  if (entry.disabled !== undefined && typeof entry.disabled !== 'boolean') {
    throw new Error(`${ERR_INVALID_PARAMS}: ${path}.disabled must be a boolean`);
  }
  if (entry.children !== undefined) {
    if (!Array.isArray(entry.children)) {
      throw new Error(`${ERR_INVALID_PARAMS}: ${path}.children must be an array`);
    }
    entry.children.forEach((child, index) =>
      assertTreeEntryShape(child, `${path}.children[${index}]`, depth + 1),
    );
  }
  // 多余字段/重复 id/__jsExpr 故意不在这里查：那是语义校验，kernel 的
  // importTree 会用与装载完全相同的一套规则拒绝（校验不复制两份）
}

function asPluginTreeExport(params: unknown): PluginTreeExport {
  const value = (params as Record<string, unknown> | null)?.['tree'];
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${ERR_INVALID_PARAMS}: tree must be an object`);
  }
  const tree = value as Record<string, unknown>;
  if (tree.version !== 1) {
    throw new Error(`${ERR_INVALID_PARAMS}: unsupported tree export version ${String(tree.version)}`);
  }
  if (!Array.isArray(tree.entries)) {
    throw new Error(`${ERR_INVALID_PARAMS}: tree.entries must be an array`);
  }
  tree.entries.forEach((entry, index) => assertTreeEntryShape(entry, `entries[${index}]`, 0));
  return value as unknown as PluginTreeExport;
}

type Handler = (params: unknown) => unknown | Promise<unknown>;

const HANDLERS: Record<MethodName, Handler> = {
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
  // plugins.*：能力门槛（树不可达 → ERR_CAPABILITY_UNAVAILABLE）在实现里统一做
  'plugins.list': () => plugins.pluginsList(),
  'plugins.enable': (p) => plugins.pluginsEnable(asString(p, 'id')),
  'plugins.disable': (p) => plugins.pluginsDisable(asString(p, 'id')),
  'plugins.updateConfig': (p) => plugins.pluginsUpdateConfig(asString(p, 'id'), asPluginConfig(p)),
  'plugins.validateConfig': (p) =>
    plugins.pluginsValidateConfig(asString(p, 'name'), asPluginConfig(p)),
  'plugins.exportTree': () => plugins.pluginsExportTree(),
  'plugins.importTree': (p) => plugins.pluginsImportTree(asPluginTreeExport(p)),
  // plugins.market.*（#708）：包名/版本的语义校验（合法 npm 名、无路径字符）
  // 在 kernel 的安装引擎里，这里只做 IPC 形状
  'plugins.market.fetchIndex': () => market.marketFetchIndex(),
  'plugins.market.install': (p) => market.marketInstall(asString(p, 'name'), asString(p, 'version')),
  'plugins.market.uninstall': (p) => market.marketUninstall(asString(p, 'name')),
  'plugins.market.getEndpoint': () => market.marketGetEndpoint(),
  'plugins.market.setEndpoint': (p) => market.marketSetEndpoint(asString(p, 'endpoint')),
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
