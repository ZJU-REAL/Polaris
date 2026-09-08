/**
 * LLM 环节（stage）的大白话名字。清单与后端 `app/core/llm/router.py` 的 STAGES
 * 一一对应（同一份 stage 标识符在设置页配路由、看用量分布）。
 *
 * 模块级常量只存 zh/en，渲染处再 tr()（见前端单语 + 中英切换约定）。
 * 插件命名空间环节（plugin:<pack>:<stage>，#736）不在这张表里——它们由插件
 * 运行时注册，前端不可能为每个插件备一份名字；渲染处统一走 stageLabel()，
 * 拿到原串照排，旁边配「插件」badge 说明来路。
 */
export const STAGE_LABELS: Record<string, { zh: string; en: string }> = {
  default: { zh: '默认', en: 'Default' },
  agent: { zh: '文献助手对话', en: 'Literature assistant chat' },
  navigator: { zh: '任务规划', en: 'Task planning' },
  sextant: { zh: '自动校验', en: 'Auto verification' },
  relevance: { zh: '相关度打分', en: 'Relevance scoring' },
  librarian: { zh: '图文精读编译', en: 'Paper compile (with figures)' },
  digest: { zh: '每日研究简报', en: 'Daily research digest' },
  extract: { zh: '结构化抽取', en: 'Structured extraction' },
  translation: { zh: '文献翻译', en: 'Literature translation' },
  reading: { zh: 'AI 伴读对话', en: 'Reading companion chat' },
  embedding: { zh: '向量嵌入', en: 'Embeddings' },
  rerank: { zh: '重排序', en: 'Reranking' },
  forge: { zh: '想法生成', en: 'Idea generation' },
  forge_generate: { zh: '候选想法生成', en: 'Candidate idea generation' },
  forge_signal: { zh: '信号摘要', en: 'Signal digest' },
  goal_explore: { zh: '目标构建', en: 'Goal building' },
  proposal: { zh: '方案起草', en: 'Proposal drafting' },
  proposal_review: { zh: '方案评审', en: 'Proposal review' },
  debate: { zh: '辩论评审', en: 'Debate review' },
  discovery_plan: { zh: '假设树探索', en: 'Hypothesis tree exploration' },
  experiment: { zh: '实验', en: 'Experiments' },
  writing: { zh: '论文撰写', en: 'Paper writing' },
  review: { zh: '论文评审', en: 'Paper review' },
  citation_intent: { zh: '引文意图分类', en: 'Citation intent classification' },
  extract_skeleton: { zh: '结构化摘要抽取', en: 'Structured summary extraction' },
  extract_method: { zh: '方法卡抽取', en: 'Method card extraction' },
  extract_gaps: { zh: '研究缺口抽取', en: 'Research gap extraction' },
  rag_expand: { zh: '库问答·查询扩展', en: 'Library Q&A · query expansion' },
  rag_rerank: { zh: '库问答·重排摘要', en: 'Library Q&A · rerank & summarize' },
  rag_answer: { zh: '库问答·作答', en: 'Library Q&A · answering' },
  hyp_generate: { zh: '假设·候选生成', en: 'Hypothesis · generation' },
  hyp_ground: { zh: '假设·文献接地', en: 'Hypothesis · grounding' },
  hyp_novelty: { zh: '假设·查新判定', en: 'Hypothesis · novelty check' },
  hyp_feasibility: { zh: '假设·可行性论证', en: 'Hypothesis · feasibility' },
  hyp_compare: { zh: '假设·两两对比', en: 'Hypothesis · pairwise compare' },
};

/**
 * 任意 stage 的展示名：内置环节用大白话名字；插件环节与未知环节照排原串
 * （插件环节的「插件」badge 由渲染处按 isPluginStage 另行补上）。
 */
export function stageLabel(stage: string): { zh: string; en: string } {
  return STAGE_LABELS[stage] ?? { zh: stage, en: stage };
}
