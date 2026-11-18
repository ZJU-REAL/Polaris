/* 能力清单。前端所有「宿主能不能做某事」的判断只读这张表。 */

import { app } from 'electron';

import {
  CAPABILITY_PLUGINS_MANAGE,
  CONTRACT_VERSION,
  type CapabilityManifest,
  type CapabilityState,
  type HostInfo,
} from '../shared/contract';
import { kernelConfigTree } from './kernel';

/**
 * 「设置 → 插件」与插件市场的总开关。现在是关的：
 * 市场里只有一个演示插件，索引里声明的七类扩展点在宿主侧一个接缝都还没有
 * （插件加不了页面、文献源、工具，引擎也看不见它），这一页对用户只有风险
 * 没有功能。代码、IPC 方法（plugins.*）和壳级 E2E 都留着；等扩展点落地，
 * 把这里改回 true 即可。开发/测试时可用 POLARIS_PLUGINS_UI=1 临时打开
 * （每次读能力清单时现看，冒烟靠它两种状态都测）。
 */
export const PLUGINS_UI_ENABLED = false;

function pluginsUiEnabled(): boolean {
  return PLUGINS_UI_ENABLED || process.env.POLARIS_PLUGINS_UI === '1';
}

/**
 * plugins.manage（#705）：总开关打开、kernel 活着且 configTree 服务可达才可用。
 * 不做缓存——树挂载失败/停机后能力要立刻翻 false，与 plugins.* 方法族的
 * 门槛（kernel 的 requireTree）读的是同一个访问函数。
 */
function pluginsManageState(): CapabilityState {
  if (!pluginsUiEnabled()) return { available: false, reason: 'plugins are not available in this version' };
  return kernelConfigTree()
    ? { available: true }
    : { available: false, reason: 'kernel config tree unavailable' };
}

export function capabilityManifest(): CapabilityManifest {
  return {
    hostVersion: app.getVersion(),
    platform: process.platform as HostInfo['platform'],
    contract: CONTRACT_VERSION,
    capabilities: {
      [CAPABILITY_PLUGINS_MANAGE]: pluginsManageState(),
    },
  };
}
