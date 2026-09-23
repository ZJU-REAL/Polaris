/* ============================================================
   模型路由表里的输入预算与上下文窗口（#811）。

   两个概念分开摆：上下文窗口是模型能收多少（token），输入预算是这个环节打算给多少
   （字符）。预算的键、默认值与上下限都来自后端登记表（GET /admin/llm/input-budgets），
   这里不另抄一份——抄一份的话，后端改了默认值，界面上显示的「默认」就在说谎。

   生效值的算法与后端 InputBudget.effective 一致：配置值（空 = 默认）夹进上下限，
   再按窗口封顶。界面据此显示「实际生效多少」，而不是只显示用户填了什么。
   ============================================================ */
import type { LlmInputBudgetSpec } from '../../lib/api';
import { tr } from '../../lib/i18n';

/** 预算键的显示名。函数而不是模块级常量：tr 要在渲染时按当前语言取值。 */
export function budgetLabel(key: string): string {
  switch (key) {
    case 'fulltext_chars':
      return tr('论文正文', 'Paper full text');
    case 'context_chars':
      return tr('知识库上下文总长', 'Total knowledge context');
    case 'excerpt_chars':
      return tr('单篇 wiki 摘录', 'Per-paper wiki excerpt');
    default:
      return key;
  }
}

/** 输入框里的字符串 → 正整数；空、非数字、非正数都当没填（null）。允许千分位逗号。 */
export function parsePositiveInt(raw: string): number | null {
  const t = raw.replace(/[,_\s]/g, '');
  if (!/^\d+$/.test(t)) return null;
  const n = Number(t);
  return n > 0 ? n : null;
}

export interface EffectiveBudget {
  value: number;
  /** 默认值被模型窗口压低了（没填时才会出现；填了的超窗口是 problem） */
  cappedByWindow: boolean;
  /**
   * 填的值保存时会被后端拒收：超出上下限（range）或超出窗口能收下的量（window）。
   * 保存前就要说出来——等 400 回来，用户已经不知道是哪一格填错了。
   */
  problem: { kind: 'range'; min: number; max: number } | { kind: 'window'; max: number } | null;
}

export function effectiveBudget(
  spec: LlmInputBudgetSpec,
  raw: string,
  contextWindow: number | null,
): EffectiveBudget {
  const configured = parsePositiveInt(raw);
  const base = configured ?? spec.default;
  const bounded = Math.min(Math.max(base, spec.minimum), spec.maximum);
  const cap = contextWindow ? contextWindow * spec.chars_per_window_token : null;
  const value = cap !== null ? Math.min(bounded, cap) : bounded;
  let problem: EffectiveBudget['problem'] = null;
  if (configured !== null && bounded !== configured) {
    problem = { kind: 'range', min: spec.minimum, max: spec.maximum };
  } else if (configured !== null && cap !== null && configured > cap) {
    problem = { kind: 'window', max: cap };
  }
  return { value, cappedByWindow: configured === null && cap !== null && cap < bounded, problem };
}

/** 草稿里的预算 → 提交给后端的对象；一个都没填时返回 undefined（= 全用默认）。 */
export function budgetsPayload(
  drafts: Record<string, string>,
  specs: LlmInputBudgetSpec[],
): Record<string, number> | undefined {
  const out: Record<string, number> = {};
  for (const spec of specs) {
    const n = parsePositiveInt(drafts[spec.key] ?? '');
    if (n !== null) out[spec.key] = n;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/** 按环节分组，设置页据此决定哪几行下面画预算输入框。 */
export function specsByStage(specs: LlmInputBudgetSpec[]): Record<string, LlmInputBudgetSpec[]> {
  const out: Record<string, LlmInputBudgetSpec[]> = {};
  for (const spec of specs) (out[spec.stage] ??= []).push(spec);
  return out;
}

export function fmtChars(n: number): string {
  return n.toLocaleString('en-US');
}
