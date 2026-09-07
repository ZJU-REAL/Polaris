/* 设置页「插件」tab 的纯展示逻辑（#707）。

   抽成独立模块有两个原因：
   ① 状态徽章映射、JSON 解析这类东西不依赖 React，抽出来 vitest 直接测；
   ② 文案不能在模块顶层 tr()（语言切换后不更新，见 lib/i18n 注释），
      所以这里一律返回 {zh, en}，由组件在渲染处再 tr。 */

import type { PluginEntryInfo, PluginTreeExport, PluginValidationError } from '../../lib/host';

export interface PluginBadge {
  zh: string;
  en: string;
  /** 徽章底色 / 文字色（design token 变量名）。 */
  bg: string;
  tx: string;
  /** 出错时把错误详情放 title（悬停可看全文）。 */
  title?: string;
}

/** 插件运行状态 → 徽章样式与文案。未知状态按「出错」兜底，宁可显眼也别静默。 */
export function pluginBadge(entry: Pick<PluginEntryInfo, 'state' | 'error'>): PluginBadge {
  switch (entry.state) {
    case 'active':
      return { zh: '运行中', en: 'Running', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' };
    case 'disabled':
      return { zh: '已停用', en: 'Disabled', bg: 'var(--surface-3)', tx: 'var(--text-3)' };
    case 'pending':
      return { zh: '启动中', en: 'Starting', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' };
    case 'error':
    default:
      return {
        zh: '出错',
        en: 'Error',
        bg: 'var(--danger-bg)',
        tx: 'var(--danger-tx)',
        ...(entry.error ? { title: entry.error } : {}),
      };
  }
}

export type ConfigParse =
  | { ok: true; config: Record<string, unknown> }
  | { ok: false; errorZh: string; errorEn: string };

/** 配置文本域 → JSON 对象。空文本视作空配置；非对象（数组/字符串等）也算错，
    因为主进程的 updateConfig 只收对象，提前在前端拦下来能给出更明白的话。 */
export function parseConfigDraft(text: string): ConfigParse {
  const trimmed = text.trim();
  if (trimmed === '') return { ok: true, config: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (e) {
    return {
      ok: false,
      errorZh: `不是合法的 JSON：${e instanceof Error ? e.message : String(e)}`,
      errorEn: `Not valid JSON: ${e instanceof Error ? e.message : String(e)}`,
    };
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {
      ok: false,
      errorZh: '配置必须是一个 JSON 对象（{ … }）',
      errorEn: 'Config must be a JSON object ({ … })',
    };
  }
  return { ok: true, config: parsed as Record<string, unknown> };
}

/** 校验错误 → 每条一行的展示文本（path 在前，没有 path 就只剩 message）。 */
export function validationErrorLines(errors: PluginValidationError[]): string[] {
  return errors.map((e) => (e.path ? `${e.path}: ${e.message}` : e.message));
}

export type TreeFileParse =
  | { ok: true; tree: PluginTreeExport }
  | { ok: false; errorZh: string; errorEn: string };

/** 导入文件内容 → 配置树载荷。只做形状检查（version=1、entries 是数组），
    每个条目的合法性由主进程校验——那边有 schema，这里重复一遍只会两处漂移。 */
export function parseTreeFile(text: string): TreeFileParse {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return {
      ok: false,
      errorZh: '这个文件不是 JSON，确认选的是之前导出的配置文件',
      errorEn: 'This file is not JSON — make sure it is a previously exported config file',
    };
  }
  const tree = parsed as { version?: unknown; entries?: unknown };
  if (tree === null || typeof tree !== 'object' || tree.version !== 1 || !Array.isArray(tree.entries)) {
    return {
      ok: false,
      errorZh: '文件格式不对：应当是「导出全部配置」生成的文件',
      errorEn: 'Unexpected file format — expected a file produced by “Export all config”',
    };
  }
  return { ok: true, tree: parsed as PluginTreeExport };
}
