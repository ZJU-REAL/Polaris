import { UnderConstruction } from '../shared/UnderConstruction';

export function PaperReviewPage() {
  return (
    <UnderConstruction
      eyebrow="Stage 05 · Paper Review"
      icon="shield"
      title="论文评审"
      titleEn="Paper Review"
      sub="形式审 + 模拟同行评审，与写作 agent 硬分离"
      subEn="Formal checks + simulated peer review, hard-separated from the writer"
      milestone="M5"
      features={[
        { zh: '形式审（form checks）', desc: '语言/术语一致性、模板与页数、逐条 cite_verify（存在性 + 论述一致性），抓虚构引用与断章取义。' },
        { zh: '模拟同行评审', desc: '多 reviewer（soundness/presentation/contribution/rating/confidence）+ meta-review 汇总推荐结论。' },
        { zh: '结构化评审意见', desc: 'strengths / weaknesses / questions 结构化输出，回灌写作阶段修订。' },
        { zh: 'Generator ≠ Reviewer', desc: '评审模型与写作模型硬分离（不同模型路由），规避自评偏差。' },
        { zh: '投稿前把关', desc: '评审通过 + 问题清零后才解锁 paper_submission 闸门。' },
      ]}
    />
  );
}
