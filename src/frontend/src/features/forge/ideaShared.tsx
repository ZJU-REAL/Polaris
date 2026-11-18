import type { IdeaScores } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   想法的四项评分和研究类型的叫法（想法页、想法详情共用）。
   ============================================================ */

/** 研究类型中文标签（大白话，界面不出现英文枚举）。 */
export const RESEARCH_TYPE_ZH: Record<string, string> = {
  method: '方法',
  benchmark: '评测基准',
  analysis: '分析',
  survey: '综述',
  application: '应用',
  theory: '理论',
};

const RESEARCH_TYPE_EN: Record<string, string> = {
  method: 'Method',
  benchmark: 'Benchmark',
  analysis: 'Analysis',
  survey: 'Survey',
  application: 'Application',
  theory: 'Theory',
};

/** 研究类型标签（按当前语言，渲染处调用）。 */
export function researchTypeLabel(type: string): string {
  const zh = RESEARCH_TYPE_ZH[type];
  return zh ? tr(zh, RESEARCH_TYPE_EN[type]) : type;
}

export interface ScoreDim {
  key: keyof IdeaScores;
  zh: string;
  en: string;
}

export const SCORE_DIMS: ScoreDim[] = [
  { key: 'novelty', zh: '新颖性', en: 'Novelty' },
  { key: 'feasibility', zh: '可行性', en: 'Feasibility' },
  { key: 'operability', zh: '可操作性', en: 'Practicality' },
  { key: 'impact', zh: '影响', en: 'Impact' },
];
