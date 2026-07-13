import { UnderConstruction } from '../shared/UnderConstruction';

export function ReviewPage() {
  return (
    <UnderConstruction
      eyebrow="Stage 02 · Idea Review"
      icon="scale"
      title="Idea 评审"
      titleEn="Idea Review"
      sub="Elo 锦标赛排序 + 语义聚类去重 + 人在环晋级闸门"
      subEn="Elo tournament ranking, semantic dedup, human promotion gate"
      milestone="M3"
      features={[
        { zh: 'Elo 锦标赛排序', desc: '成对比较的相对评分代替单点绝对打分，规避 LLM 单点打分噪声，保留对战理由可回溯。' },
        { zh: '语义聚类去重', desc: 'embedding 聚类对候选 idea 去重，每簇仅保留 Elo 最高者，防止高分 idea 同质化坍缩。' },
        { zh: '晋级闸门（人在环）', desc: 'candidate → accepted 是唯一人工晋级点：创建 Gate 记录并暂停，审批通过后进入实验阶段。' },
        { zh: '评分详情与雷达', desc: 'novelty/feasibility/operability/impact 分项展示、最近邻 prior 与 novelty verdict。' },
        { zh: '淘汰记录', desc: 'rejected idea 保留淘汰原因（如 not-novel、低于阈值），供后续 run 借鉴。' },
      ]}
    />
  );
}
