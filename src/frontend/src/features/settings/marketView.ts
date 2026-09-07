/* 设置页插件市场分区的纯展示逻辑（#711，插件市场计划 PR-7）。

   与 pluginsView.ts 同理由抽出：徽章映射、权限文案、安装态机都不依赖
   React，抽出来 vitest 直接测；文案一律返回 {zh, en}，渲染处再 tr
   （模块顶层 tr 不会随语言切换更新，见 lib/i18n 注释）。 */

import type { MarketIndexEntry, PluginEntryInfo } from '../../lib/host';

export interface BiText {
  zh: string;
  en: string;
}

/** 插件种类 → 大白话徽章文案。未知种类原样展示（新种类先上索引也不至于空白）。 */
export function marketKindLabel(kind: MarketIndexEntry['kind'] | string): BiText {
  switch (kind) {
    case 'datasource':
      return { zh: '数据源', en: 'Data source' };
    case 'record-kind':
      return { zh: '记录类型', en: 'Record kind' };
    case 'runner':
      return { zh: '运行器', en: 'Runner' };
    case 'agent-tool':
      return { zh: 'AI 工具', en: 'Agent tool' };
    case 'workflow':
      return { zh: '工作流', en: 'Workflow' };
    case 'discipline':
      return { zh: '学科包', en: 'Discipline' };
    case 'panel':
      return { zh: '面板', en: 'Panel' };
    default:
      return { zh: kind, en: kind };
  }
}

export interface TierBadge extends BiText {
  /** 徽章底色 / 文字色（design token 变量名）。 */
  bg: string;
  tx: string;
}

/** 质量分级 → 徽章。明度上白金最亮眼（accent），铜最低调，与 tier 语义同向。 */
export function marketTierBadge(tier: MarketIndexEntry['tier'] | string): TierBadge {
  switch (tier) {
    case 'platinum':
      return { zh: '白金', en: 'Platinum', bg: 'var(--accent-soft)', tx: 'var(--accent-text)' };
    case 'gold':
      return { zh: '金牌', en: 'Gold', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' };
    case 'silver':
      return { zh: '银牌', en: 'Silver', bg: 'var(--surface-3)', tx: 'var(--text-2)' };
    case 'bronze':
      return { zh: '铜牌', en: 'Bronze', bg: 'var(--surface-3)', tx: 'var(--text-3)' };
    default:
      return { zh: tier, en: tier, bg: 'var(--surface-3)', tx: 'var(--text-3)' };
  }
}

/** 权限声明 → 如实展示的标签列表。v1 只展示声明、不做强制限制（诚实原则），
    这句话由组件在权限区固定附注，不在这里拼。 */
export function marketPermissionLabels(permissions: MarketIndexEntry['permissions']): BiText[] {
  const labels: BiText[] = [];
  if (permissions.network) labels.push({ zh: '声明需要联网', en: 'Declares network access' });
  if (permissions.filesystem) labels.push({ zh: '声明需要读写文件', en: 'Declares file access' });
  if (labels.length === 0) labels.push({ zh: '未声明任何权限', en: 'No permissions declared' });
  return labels;
}

/** 安装 job 的四阶段 → 进度文字。阶段名以 desktop 侧 PHASE_STEP 为准
    （download/verify/extract/register，分母恒为 4）；未知阶段原样展示。 */
export function installPhaseText(phase: string): BiText {
  switch (phase) {
    case 'download':
      return { zh: '下载中', en: 'Downloading' };
    case 'verify':
      return { zh: '校验中', en: 'Verifying' };
    case 'extract':
      return { zh: '解压中', en: 'Extracting' };
    case 'register':
      return { zh: '登记中', en: 'Registering' };
    default:
      return { zh: phase, en: phase };
  }
}

export interface InstallProgress {
  phase: string;
  done: number;
  total: number;
}

/** npm 包名 → 配置树条目 id。必须与 kernel 的 packageEntryId 逐字符同步
    （src/kernel/src/market/lifecycle.ts）：':' 是 loader 子树分隔符、'/' 读起来
    像路径，都不能进 id，scoped 包剥 @、'/' 换 '--'。前端不能跨目录 import
    kernel（镜像惯例，见 lib/host.ts 头注释），所以这里手工镜像一份。 */
export function normalizePackageId(name: string): string {
  return name.replace(/^@/, '').replace(/\//g, '--');
}

/** 市场条目在本机的状态。装/启是两步，中间态（已装未启）必须显式存在。 */
export type MarketEntryState =
  | { kind: 'not-installed' }
  | { kind: 'installing'; progress: InstallProgress }
  | { kind: 'installed-disabled'; pluginId: string }
  | { kind: 'installed-enabled'; pluginId: string };

/**
 * 态机：市场条目名 + 已装插件列表 + 进行中的安装 → 当前状态。
 *
 * - 匹配按条目 id：市场安装登记的树条目 name 存的是 file:// 入口 URL，
 *   id 才是规范化包名（kernel packageEntryId），按 name 匹配永远不中。
 * - 安装进行中优先于「已装」：升级/重装时列表里可能已有同名旧条目，
 *   此刻用户关心的是进度，不是旧条目的开关。
 * - 命中多条时只要有一个没停用就算「已启用」——宁可少催一次「启用」，
 *   也别在已经跑着的情况下再给启用按钮。
 */
export function marketEntryState(
  entryName: string,
  installedPlugins: Pick<PluginEntryInfo, 'id' | 'name' | 'disabled'>[],
  installing: Record<string, InstallProgress | undefined>,
): MarketEntryState {
  const progress = installing[entryName];
  if (progress) return { kind: 'installing', progress };
  const entryId = normalizePackageId(entryName);
  const matches = installedPlugins.filter((p) => p.id === entryId);
  const enabled = matches.find((p) => !p.disabled);
  if (enabled) return { kind: 'installed-enabled', pluginId: enabled.id };
  const first = matches[0];
  if (!first) return { kind: 'not-installed' };
  return { kind: 'installed-disabled', pluginId: first.id };
}
