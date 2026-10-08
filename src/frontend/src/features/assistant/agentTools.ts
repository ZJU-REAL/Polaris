import type { IconName } from '../../components/ui/Icon';
import type { AssistantBlock } from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';

/* ============================================================
   外部 agent（#836）在对话里的样子。

   agent 的工具调用统一叫 agent_<kind>（read / edit / execute …），具体干了什么写在
   标题里（"Read /path/notes.txt"）。卡片上显示标题，再配一个按类别的中文标签和图标；
   Polaris 自己的工具一律原样显示，这里不碰。
   ============================================================ */

export type AgentToolKind =
  | 'read' | 'edit' | 'execute' | 'search' | 'fetch' | 'think' | 'delete' | 'move' | 'other';

// 模块级只存中英两份，tr() 在调用时求值（切语言后才会跟着变）
const KIND_META: Record<AgentToolKind, { zh: string; en: string; icon: IconName }> = {
  read: { zh: '读取', en: 'Read', icon: 'file' },
  edit: { zh: '修改', en: 'Edit', icon: 'pen' },
  execute: { zh: '运行命令', en: 'Run', icon: 'cpu' },
  search: { zh: '搜索', en: 'Search', icon: 'search' },
  fetch: { zh: '联网获取', en: 'Fetch', icon: 'download' },
  think: { zh: '思考', en: 'Think', icon: 'bulb' },
  delete: { zh: '删除', en: 'Delete', icon: 'trash' },
  move: { zh: '移动', en: 'Move', icon: 'arrow' },
  other: { zh: '操作', en: 'Action', icon: 'sparkle' },
};

/** agent_<kind> → kind；不是外部 agent 的工具返回 null。认不出的 kind 归到 other。 */
export function agentToolKind(name: string): AgentToolKind | null {
  if (!name.startsWith('agent_')) return null;
  const kind = name.slice('agent_'.length);
  return kind in KIND_META ? (kind as AgentToolKind) : 'other';
}

export function agentToolLabel(kind: AgentToolKind): string {
  const meta = KIND_META[kind];
  return tr(meta.zh, meta.en);
}

export function agentToolIcon(kind: AgentToolKind): IconName {
  return KIND_META[kind].icon;
}

type ToolBlock = Extract<AssistantBlock, { kind: 'tool' }>;

/** 卡片 / 状态行上给人看的工具名：外部 agent 用标题（没有就用结果摘要、再没有就用类别），
    Polaris 工具保持原名。 */
export function toolDisplayName(block: Pick<ToolBlock, 'name' | 'title' | 'summary'>): string {
  const kind = agentToolKind(block.name);
  if (!kind) return block.name;
  return block.title?.trim() || block.summary?.trim() || agentToolLabel(kind);
}
