/* ============================================================
   Polaris — mock data (M1 前端骨架用)
   从设计原型的数据模型精选移植并 TypeScript 类型化。
   后端就绪后由 TanStack Query + lib/api 替换。
   ============================================================ */

import type { IconName } from '../components/ui/Icon';

// ---- Pipeline stages ----

export interface PipelineStage {
  key: string;
  path: string;
  no: string;
  icon: IconName;
  zh: string;
  en: string;
  count: number;
  /** 当前有任务运行中的阶段 */
  running?: boolean;
}

export const pipelineStages: PipelineStage[] = [
  { key: 'wiki', path: '/wiki', no: '00', icon: 'book', zh: '文献追踪', en: 'Research Wiki', count: 138 },
  { key: 'forge', path: '/forge', no: '01', icon: 'bulb', zh: 'Idea 生成', en: 'Idea Forge', count: 6 },
  { key: 'review', path: '/review', no: '02', icon: 'scale', zh: 'Idea 评审', en: 'Idea Review', count: 2 },
  { key: 'experiment', path: '/experiment', no: '03', icon: 'flask', zh: '实验搭建', en: 'Experiment Lab', count: 2, running: true },
  { key: 'writer', path: '/writer', no: '04', icon: 'pen', zh: '论文撰写', en: 'Paper Writer', count: 1 },
  { key: 'paper-review', path: '/paper-review', no: '05', icon: 'shield', zh: '论文评审', en: 'Paper Review', count: 1 },
];

// ---- Research direction (active vault) ----

export interface Direction {
  slug: string;
  title: string;
  titleEn: string;
}

export const direction: Direction = {
  slug: 'llm-agentic-research',
  title: 'LLM 自主科研智能体',
  titleEn: 'LLM Agentic Research',
};

// ---- Dashboard stat cards ----

export interface Stat {
  icon: IconName;
  label: string;
  en: string;
  value: string;
  sub?: string;
  accent?: boolean;
}

export const stats: Stat[] = [
  { icon: 'book', label: '知识库论文', en: 'Papers in vault', value: '138', sub: '+3 今日' },
  { icon: 'bulb', label: 'Idea 候选池', en: 'Idea candidates', value: '6', sub: '4 candidate' },
  { icon: 'flask', label: '实验 GPU·h', en: 'Compute used', value: '31.4', sub: '/ 48' },
  { icon: 'gate', label: '待审批闸门', en: 'Pending gates', value: '3', sub: '人在环', accent: true },
];

// ---- Activity feed ----

export interface Activity {
  t: string;
  skill: string;
  icon: IconName;
  text: string;
  live?: boolean;
  gate?: boolean;
}

export const activities: Activity[] = [
  { t: '19:05', skill: 'experiment-lab', icon: 'flask', text: 'run-006 已提交至 gpu-node-1（slurm job 88213），流式拉日志中', live: true },
  { t: '19:40', skill: 'approvals', icon: 'gate', text: '新审批：投稿 NeurIPS 2026 待你确认', gate: true },
  { t: '18:52', skill: 'approvals', icon: 'gate', text: '你已批准远程写操作 G-20260529-001' },
  { t: '13:20', skill: 'experiment-lab', icon: 'flask', text: 'run-005 keep：novelty-precision 0.830（+9.4pt vs baseline）' },
  { t: '11:30', skill: 'paper-review', icon: 'shield', text: 'cite_verify 抓到 1 处虚构引用、1 处断章取义' },
  { t: '09:14', skill: 'research-wiki', icon: 'book', text: 'ingest 日更：新增 3 篇相关论文，水位 → 2026-05-29' },
  { t: '08:40', skill: 'idea-forge', icon: 'bulb', text: 'run 完成：8 存活 → 6 candidate 入库' },
];

// ---- Approval gates (human-in-the-loop) ----

export type GateStatus = 'pending' | 'approved' | 'rejected';

export type GateType =
  | 'idea_promotion'
  | 'compute_budget'
  | 'remote_write'
  | 'pr_push'
  | 'paper_submission';

export const GATE_TYPE_ZH: Record<GateType, string> = {
  idea_promotion: 'Idea 晋级',
  compute_budget: '算力预算',
  remote_write: '远程写操作',
  pr_push: '推送 PR',
  paper_submission: '论文投稿',
};

export interface Gate {
  id: string;
  type: GateType;
  skill: string;
  idea: string;
  status: GateStatus;
  title: string;
  desc: string;
  created: string;
  urgent?: boolean;
  decidedBy?: string;
  decidedAt?: string;
  payload: string[];
}

export const gates: Gate[] = [
  {
    id: 'G-20260529-004', type: 'paper_submission', skill: 'paper-writer', idea: 'I-20260529-003',
    status: 'pending', urgent: true, created: '2026-05-29 19:40',
    title: '投稿 NeurIPS 2026',
    desc: 'main.pdf 已编译（8 页）、bib 全部 SS 校验通过、形式审有 2 处引用问题待修。',
    payload: ['venue: neurips2026', 'anonymized: true', 'deadline: 2026-05-22 (已过 → workshop track)'],
  },
  {
    id: 'G-20260529-002', type: 'idea_promotion', skill: 'idea-forge', idea: 'I-20260529-001',
    status: 'pending', created: '2026-05-29 16:12',
    title: '晋级 idea 为 accepted',
    desc: 'Elo 排序 #2 候选，composite 7.66。晋级后进入 experiment-lab。',
    payload: ['elo: 1561', 'composite: 7.66', 'next: experiment-lab plan'],
  },
  {
    id: 'G-20260529-003', type: 'compute_budget', skill: 'experiment-lab', idea: 'I-20260529-001',
    status: 'pending', created: '2026-05-29 17:05',
    title: '算力预算确认 48 GPU·h',
    desc: 'plan 已就绪：3 个 baseline、主指标 diversity@k。消耗真实算力前需确认。',
    payload: ['host: gpu-node-1', 'scheduler: slurm', 'budget: 48 GPU·h', 'per-baseline cap: 8 GPU·h'],
  },
  {
    id: 'G-20260529-001', type: 'remote_write', skill: 'experiment-lab', idea: 'I-20260529-003',
    status: 'approved', created: '2026-05-29 18:50', decidedBy: 'you', decidedAt: '2026-05-29 18:52',
    title: '远程写操作：提交 run-006 作业',
    desc: '向 gpu-node-1 rsync code/ 并 sbatch 提交。只读探查已自动放行。',
    payload: ['host: gpu-node-1', 'op: rsync + sbatch', 'isolated: conda env exp-003'],
  },
  {
    id: 'G-20260528-006', type: 'pr_push', skill: 'idea-to-pr', idea: 'I-20260528-007',
    status: 'approved', created: '2026-05-28 11:20', decidedBy: 'you', decidedAt: '2026-05-28 12:01',
    title: '推送 PR 到 open-source-proj',
    desc: '1 个改进、过测试、benchmark 不退化。',
    payload: ['repo: open-source-proj', 'branch: feat/baseline-sched', 'tests: 142 passed'],
  },
];

// ---- Featured idea (dashboard hero card) ----

export type IdeaStatus =
  | 'candidate'
  | 'accepted'
  | 'implemented'
  | 'drafted'
  | 'reviewed'
  | 'rejected';

export interface FeaturedIdea {
  id: string;
  title: string;
  titleEn: string;
  status: IdeaStatus;
  composite: number;
  elo: number;
  metric: { name: string; value: number; delta: string };
  stagesDone: number;
}

export const featuredIdea: FeaturedIdea = {
  id: 'I-20260529-003',
  title: '用 citation-graph 重排序抑制 idea 生成的新颖性幻觉',
  titleEn: 'Citation-graph reranking to suppress novelty hallucination in idea generation',
  status: 'implemented',
  composite: 7.74,
  elo: 1576,
  metric: { name: 'novelty-precision', value: 0.83, delta: '+9.4pt' },
  stagesDone: 5,
};
