import { UnderConstruction } from '../shared/UnderConstruction';

export function ExperimentPage() {
  return (
    <UnderConstruction
      eyebrow="Stage 03 · Experiment Lab"
      icon="flask"
      title="实验搭建"
      titleEn="Experiment Lab"
      sub="计划-执行-判定的自主实验循环，远程算力与预算闸门"
      subEn="Plan → run → verdict loop on remote compute with budget gates"
      milestone="M4"
      features={[
        { zh: '实验计划（plan）', desc: '先定主指标、baseline 复现优先级（官方代码 > 第三方 > 自重写 > 仅引用数字）、停止条件与 GPU·h 预算。' },
        { zh: '远程执行（SSH/Slurm）', desc: 'asyncssh 连接 GPU 节点、rsync 代码、sbatch 提交；所有远程写操作必须过 remote_write 闸门。' },
        { zh: 'Run 循环与判定', desc: 'run 列表、diff 描述、指标结果与 keep/discard 判定；WebSocket 流式拉取训练日志。' },
        { zh: '算力预算闸门', desc: '消耗真实算力前需 compute_budget 人工确认，单 baseline 预算上限防止烧光算力。' },
        { zh: '消融与结果汇总', desc: '消融表、指标曲线、最优配置沉淀，供论文撰写阶段直接引用。' },
      ]}
    />
  );
}
