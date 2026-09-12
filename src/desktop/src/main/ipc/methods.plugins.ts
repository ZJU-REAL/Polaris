/* ============================================================
   plugins.* 在桌面侧的绑定（#705，#754 起实现搬进 kernel）。

   语义与参数守卫都住在 @polaris/kernel 的 rpc/plugin-methods.ts——服务器
   形态要的是同一批方法、同一套校验，只是传输不同，所以实现只有一份。
   这里剩下的唯一职责是把桌面的宿主句柄（模块级单例）接上去。

   句柄传的是函数不是值：树与持久层在会话中可能尚未就绪（storage 挂载失败、
   树装载失败），能力门槛必须每次调用现读。
   ============================================================ */

import { createPluginMethods, type RpcMethod } from '@polaris/kernel';

import type { MethodName, PluginEntryInfo, PluginTreeExport } from '../../shared/contract';
import { kernelConfigTree, kernelPluginMeta } from '../kernel';

/**
 * 本表覆盖的方法名：plugins.* 去掉 plugins.market.*（市场那族还在桌面侧，
 * 它要 job 事件与旧 store 读穿）。写成受约束的子集而不是 Record<string>，
 * 是为了保住 router 那张表的穷尽性——契约里新增一个 plugins.* 方法却没实现，
 * 应该当场编译失败，而不是运行时才报未知方法。
 */
type SharedPluginMethod = Exclude<
  Extract<MethodName, `plugins.${string}`>,
  `plugins.market.${string}`
>;

export const pluginMethods = createPluginMethods({
  configTree: () => kernelConfigTree(),
  pluginMeta: () => kernelPluginMeta(),
}) as Record<SharedPluginMethod, RpcMethod>;

/* 具名直调入口：冒烟测试在主进程内不经 IPC 直接驱动这几个方法，保留带类型的
   包装让它免去自己拼参数对象。生产路径（router）走上面那张表。 */

export function pluginsList(): PluginEntryInfo[] {
  return pluginMethods['plugins.list'](undefined) as PluginEntryInfo[];
}

export function pluginsEnable(id: string): Promise<PluginEntryInfo> {
  return pluginMethods['plugins.enable']({ id }) as Promise<PluginEntryInfo>;
}

export function pluginsDisable(id: string): Promise<PluginEntryInfo> {
  return pluginMethods['plugins.disable']({ id }) as Promise<PluginEntryInfo>;
}

export function pluginsExportTree(): PluginTreeExport {
  return pluginMethods['plugins.exportTree'](undefined) as PluginTreeExport;
}
