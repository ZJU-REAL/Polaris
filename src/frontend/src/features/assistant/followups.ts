import type { AssistantBlock } from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';

/* ============================================================
   答完之后的几条追问。

   **从这一轮真实发生的事里出**，不凭空造：查到了论文就问它们的共同点，取过图就问
   图说明了什么，排过计划就问下一步。凭空生成的建议看起来聪明，但十条里有九条问的是
   这轮根本没碰过的东西——用户点一次就再也不点了。

   最多三条：建议是台阶，不是菜单。
   ============================================================ */

export function followUps(blocks: AssistantBlock[]): string[] {
  const out: string[] = [];
  const tools = blocks.filter((b) => b.kind === 'tool');
  const sources = blocks.find((b) => b.kind === 'sources');
  const plan = blocks.find((b) => b.kind === 'plan');

  if (sources?.kind === 'sources' && sources.papers.length >= 2) {
    out.push(tr('这几篇的共同点和分歧在哪？', 'What do these papers agree and disagree on?'));
    out.push(tr('挑最相关的一篇，帮我精读', 'Pick the most relevant one and read it closely'));
  }
  if (tools.some((b) => b.kind === 'tool' && b.name.includes('figure'))) {
    out.push(tr('这些图说明了什么问题？', 'What do these figures actually show?'));
  }
  if (plan?.kind === 'plan' && plan.steps.some((s) => s.status !== 'done')) {
    out.push(tr('接着做下一步', 'Continue with the next step'));
  }
  if (tools.some((b) => b.kind === 'tool' && b.name.includes('experiment'))) {
    out.push(tr('下一步实验该怎么设计？', 'How should the next experiment be designed?'));
  }
  // 一条都没有时不硬凑：没有台阶好过给一个假台阶
  return out.slice(0, 3);
}
