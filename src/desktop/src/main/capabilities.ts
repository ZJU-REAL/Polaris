/* 能力清单。前端所有「走本地还是走远端」的判断只读这张表。

   latex.compile 目前 available: false —— 本地编译还没实现，但 tectonic 的
   探测是真的在跑：落地时把 available 翻成探测结果即可，前端判断逻辑不用改。
   plugins.manage（#705）是第一个真的翻了 true 的能力位。 */

import { app } from 'electron';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

import {
  CAPABILITY_LATEX_COMPILE,
  CAPABILITY_PLUGINS_MANAGE,
  CONTRACT_VERSION,
  type CapabilityManifest,
  type CapabilityState,
  type HostInfo,
} from '../shared/contract';
import { kernelConfigTree } from './kernel';

const execFileAsync = promisify(execFile);
const NOT_IMPLEMENTED_REASON = 'not implemented yet';

/**
 * plugins.manage（#705）：kernel 活着且 configTree 服务可达才可用。
 * 不做缓存——树挂载失败/停机后能力要立刻翻 false，与 plugins.* 方法族的
 * 门槛（methods.plugins.ts 的 requireTree）读的是同一个访问函数。
 */
function pluginsManageState(): CapabilityState {
  return kernelConfigTree()
    ? { available: true }
    : { available: false, reason: 'kernel config tree unavailable' };
}

let tectonicProbe: CapabilityState['detail'] | undefined;
let probed = false;

async function probeTectonic(): Promise<void> {
  if (probed) return;
  probed = true;
  try {
    const { stdout } = await execFileAsync('tectonic', ['--version'], { timeout: 3_000 });
    tectonicProbe = { found: true, version: stdout.trim() };
  } catch {
    tectonicProbe = { found: false };
  }
}

export async function capabilityManifest(): Promise<CapabilityManifest> {
  await probeTectonic();
  return {
    hostVersion: app.getVersion(),
    platform: process.platform as HostInfo['platform'],
    contract: CONTRACT_VERSION,
    capabilities: {
      // detail 里已经带上了「本机有没有 tectonic」，本地编译落地时把 available
      // 翻成 found 即可，前端判断逻辑一行不用改。
      [CAPABILITY_LATEX_COMPILE]: {
        available: false,
        reason: NOT_IMPLEMENTED_REASON,
        detail: tectonicProbe,
      },
      [CAPABILITY_PLUGINS_MANAGE]: pluginsManageState(),
    },
  };
}
