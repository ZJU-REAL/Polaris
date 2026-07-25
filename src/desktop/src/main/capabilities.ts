/* 能力清单。前端所有「走本地还是走远端」的判断只读这张表。

   一期全部 available: false —— 本地能力还没实现。但 tectonic 的探测是真的做了，
   一来演练探测这条路，二来第二期接本地编译时这段不用重写。 */

import { app } from 'electron';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

import {
  CAPABILITY_LATEX_COMPILE,
  CAPABILITY_PAPER_IMPORT,
  CAPABILITY_PDF_CACHE,
  CONTRACT_VERSION,
  type CapabilityManifest,
  type CapabilityState,
  type HostInfo,
} from '../shared/contract';

const execFileAsync = promisify(execFile);
const PHASE_1_REASON = 'not implemented yet (phase 1 ships the shell only)';

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
      // detail 里已经带上了「本机有没有 tectonic」，第二期把 available 翻成
      // found 即可，前端判断逻辑一行不用改。
      [CAPABILITY_LATEX_COMPILE]: {
        available: false,
        reason: PHASE_1_REASON,
        detail: tectonicProbe,
      },
      [CAPABILITY_PAPER_IMPORT]: { available: false, reason: PHASE_1_REASON },
      [CAPABILITY_PDF_CACHE]: { available: false, reason: PHASE_1_REASON },
    },
  };
}
