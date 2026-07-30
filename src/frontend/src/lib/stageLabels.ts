/**
 * LLM 环节（stage）的大白话名字。清单与后端 `app/core/llm/router.py` 的 STAGES
 * 一一对应（同一份 stage 标识符在设置页配路由、在实验室页看用量分布）。
 *
 * 模块级常量只存 zh/en，渲染处再 tr()（见前端单语 + 中英切换约定）。
 */
export const STAGE_LABELS: Record<string, { zh: string; en: string }> = {
  default: { zh: '默认', en: 'Default' },
  navigator: { zh: '任务规划', en: 'Task planning' },
  sextant: { zh: '自动校验', en: 'Auto verification' },
  relevance: { zh: '相关度打分', en: 'Relevance scoring' },
  librarian: { zh: '图文精读编译', en: 'Paper compile (with figures)' },
  digest: { zh: '每日研究简报', en: 'Daily research digest' },
  extract: { zh: '结构化抽取', en: 'Structured extraction' },
  reading: { zh: 'AI 伴读对话', en: 'Reading companion chat' },
  embedding: { zh: '向量嵌入', en: 'Embeddings' },
  rerank: { zh: '重排序', en: 'Reranking' },
  forge: { zh: '想法生成', en: 'Idea generation' },
  forge_signal: { zh: '信号摘要', en: 'Signal digest' },
  goal_explore: { zh: '目标构建', en: 'Goal building' },
  proposal: { zh: '方案起草', en: 'Proposal drafting' },
  proposal_review: { zh: '方案评审', en: 'Proposal review' },
  debate: { zh: '辩论评审', en: 'Debate review' },
  experiment: { zh: '实验', en: 'Experiments' },
  writing: { zh: '论文撰写', en: 'Paper writing' },
  review: { zh: '论文评审', en: 'Paper review' },
  feedback_issue: { zh: '反馈整理', en: 'Feedback triage' },
};

/** 取环节的显示名；未知 stage（如历史用量里的旧标识符）回退为标识符本身。 */
export function stageLabel(stage: string): { zh: string; en: string } {
  return STAGE_LABELS[stage] ?? { zh: stage, en: stage };
}
