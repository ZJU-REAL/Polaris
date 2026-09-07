/* ============================================================
   plugins.* 的实现（#705）：全部是「取 configTree 句柄 → 调 kernel 侧
   方法」的薄壳。语义都住在 kernel（@polaris/kernel 的 SqliteTree）里，
   这里只负责三件事：
   - 能力门槛：树不可达时统一抛 ERR_CAPABILITY_UNAVAILABLE（与能力位
     plugins.manage 读同一个 kernelConfigTree()，可用性天然一致）；
   - 把校验错误按契约作为数据返回（PluginValidationResult），把装载
     失败作为异常抛出——前者是用户输入问题要就地回显，后者是运行故障；
   - 写操作后 flush：树的 write 是防抖合并的，IPC 返回前落盘，renderer
     看到成功即已持久。
   参数形状校验在 router 的手写守卫里（见 router.ts 文件头），这里默认
   拿到的已是形状正确的值；语义校验（__jsExpr/未知字段/重复 id/schema）
   由 kernel 层完成——双层校验各管一层，互不重复。
   ============================================================ */

import type { SqliteTree } from '@polaris/kernel';

import {
  ERR_CAPABILITY_UNAVAILABLE,
  ERR_INVALID_PARAMS,
  type PluginEntryInfo,
  type PluginTreeExport,
  type PluginValidationResult,
} from '../../shared/contract';
import { kernelConfigTree, kernelPluginMeta } from '../kernel';

function requireTree(): SqliteTree {
  const tree = kernelConfigTree();
  if (!tree) {
    throw new Error(`${ERR_CAPABILITY_UNAVAILABLE}: plugins.manage — kernel 配置树不可用`);
  }
  return tree;
}

/** 变更后的条目回执：让 UI 开关不用再发一次 list 就能拿到最终运行态。 */
function entryInfo(tree: SqliteTree, id: string): PluginEntryInfo {
  const info = tree.listEntries().find((entry) => entry.id === id);
  // resolve 成功过的 id 一定在列表里；防御性兜底只为类型收窄
  if (!info) throw new Error(`${ERR_INVALID_PARAMS}: unknown plugin entry ${id}`);
  return info;
}

export function pluginsList(): PluginEntryInfo[] {
  return requireTree().listEntries();
}

export async function pluginsEnable(id: string): Promise<PluginEntryInfo> {
  const tree = requireTree();
  // disabled: null = 删除该键（loader 的 isNullable 语义），回到默认启用。
  // 拉起失败时 Entry.update 自己回滚 options（树不被污染），错误照抛。
  await tree.update(id, { disabled: null });
  await tree.flush();
  return entryInfo(tree, id);
}

export async function pluginsDisable(id: string): Promise<PluginEntryInfo> {
  const tree = requireTree();
  await tree.update(id, { disabled: true });
  await tree.flush();
  return entryInfo(tree, id);
}

export async function pluginsUpdateConfig(
  id: string,
  config: unknown,
): Promise<PluginValidationResult> {
  const tree = requireTree();
  const entry = tree.resolve(id); // 未知 id 在这里抛错
  if (entry.options.group) {
    // 组条目的 config 是子条目列表（loader 内部表示），从这里改会拆树
    throw new Error(`${ERR_INVALID_PARAMS}: entry ${id} is a group; groups carry no plugin config`);
  }
  const result = tree.validateConfig(entry.options.name, config);
  // 校验错误是数据不是异常：ok=false 时什么都没改，前端就地回显
  if (!result.ok) return result;
  await tree.update(id, { config });
  await tree.flush();
  return result;
}

export function pluginsValidateConfig(name: string, config: unknown): PluginValidationResult {
  return requireTree().validateConfig(name, config);
}

export function pluginsExportTree(): PluginTreeExport {
  // version 是 IPC 契约的事：kernel 只管树本身，这里包上版本号
  return { version: 1, entries: requireTree().exportTree() };
}

export async function pluginsImportTree(tree: PluginTreeExport): Promise<void> {
  // last-good 快照落 PluginMetaStore；storage 失败的内存树会话拿不到
  // meta，导入照常（本次会话本来就不落盘），见 kernel.ts 的说明
  await requireTree().importTree(tree.entries, kernelPluginMeta() ?? undefined);
}
