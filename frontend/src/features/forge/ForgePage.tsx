import { UnderConstruction } from '../shared/UnderConstruction';

export function ForgePage() {
  return (
    <UnderConstruction
      eyebrow="Stage 01 · Idea Forge"
      icon="bulb"
      title="Idea 生成"
      titleEn="Idea Forge"
      sub="generator × reviewer 分离的多轮生成-评审-反思循环"
      subEn="Multi-round generate → review → refine loop with hard generator/reviewer separation"
      milestone="M3"
      features={[
        { zh: '多轮生成-评审循环', desc: '每轮 generator 产出接地于文献的 raw ideas，reviewer×3 集成评审淘汰，读批评后反思 refine（默认 3 轮）。' },
        { zh: 'Citation-graph 新颖性查重', desc: '强制走 Semantic Scholar 引用图判定新颖性（禁纯关键词），抑制“看似新实则已有”的新颖性幻觉。' },
        { zh: 'Rubric 加权评分', desc: 'novelty/feasibility/operability/impact 加权 composite，低于闸门阈值（7.0）不入候选池。' },
        { zh: '运行过程可观测', desc: 'run 轮次日志、存活曲线、SSE 流式输出 agent 推理过程。' },
        { zh: '候选池入库', desc: '存活 idea 写入方向 vault，进入 Stage 02 排序与晋级。' },
      ]}
    />
  );
}
