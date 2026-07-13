import { UnderConstruction } from '../shared/UnderConstruction';

export function WikiPage() {
  return (
    <UnderConstruction
      eyebrow="Stage 00 · Research Wiki"
      icon="book"
      title="文献追踪"
      titleEn="Research Wiki"
      sub="每日自动抓取、综合前沿文献，多方向知识库复利增长"
      subEn="Daily ingest & synthesis of literature into per-direction vaults"
      milestone="M2"
      features={[
        { zh: 'arXiv 每日增量抓取', desc: '按方向的 categories/keywords 抓取，水位线去重、确定性 worker 执行，冷启动回溯 bootstrap 窗口。' },
        { zh: 'LLM 摘要与相关度打分', desc: '对新论文生成中英 TLDR、方法要点、与本方向的关联（relation/take），低于 relevance 阈值自动过滤。' },
        { zh: '概念实体页与交叉链接', desc: 'concepts/ 实体页（方法/架构/问题），与论文、idea 双向 wikilink，构成可导航知识图。' },
        { zh: '综述与雪球检索', desc: '追踪 survey、沿引用图做 snowball 扩展检索，补全方向主干文献。' },
        { zh: '多方向 vault 管理', desc: '方向 registry（关键词/阈值/目标会议），列表-详情 split 视图浏览与全文搜索。' },
      ]}
    />
  );
}
