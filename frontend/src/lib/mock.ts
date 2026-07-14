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
