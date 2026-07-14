import type { DiagnosticRule } from '../../lib/api';

/* ============================================================
   Paper Writer 共享小件：协作者颜色 hash、诊断规则文案、
   论文分节的大白话名称。
   ============================================================ */

/** 协作者光标/头像颜色盘（与浙大蓝主题协调的中低饱和色）。 */
const PEER_COLORS = [
  '#0d4a94', // 求是蓝
  '#3f8f5f', // 绿
  '#b4892f', // 金
  '#7263b0', // 紫
  '#1f7fa8', // 青
  '#c14f44', // 红
  '#5b8a3c', // 橄榄
  '#a05286', // 玫紫
];

/** 用户名 → 稳定颜色（简单字符串 hash 取模）。 */
export function colorForUser(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i += 1) {
    h = (h * 31 + name.charCodeAt(i)) >>> 0;
  }
  return PEER_COLORS[h % PEER_COLORS.length]!;
}

/** 编译诊断规则 → 大白话。 */
export function ruleText(rule: DiagnosticRule | string): string {
  const map: Record<string, string> = {
    undefined_citation: '引用没找到',
    undefined_reference: '交叉引用没找到',
    latex_error: 'LaTeX 错误',
    overfull: '排版溢出',
    other: '其他',
  };
  return map[rule] ?? rule;
}

/** 论文分节 key → 中文（模板 sections / AI 起草选节用）。 */
export function sectionText(key: string): string {
  const map: Record<string, string> = {
    abstract: '摘要',
    introduction: '引言',
    related_work: '相关工作',
    method: '方法',
    experimental_setup: '实验设置',
    experiments: '实验',
    results: '实验结果',
    conclusion: '结论',
  };
  return map[key] ?? key;
}

/** AI 起草的默认分节（模板未返回 sections 时的兜底，顺序同后端管线）。 */
export const DEFAULT_SECTIONS = [
  'abstract',
  'introduction',
  'related_work',
  'method',
  'experimental_setup',
  'results',
  'conclusion',
];
