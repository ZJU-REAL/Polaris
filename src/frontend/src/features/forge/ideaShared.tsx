import { ScoreRing } from '../../components/ui/ScoreRing';
import type { IdeaDepth, IdeaScores } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   Idea 四维评分共享工具（Forge / Review / Dashboard 共用）：
   维度定义、composite、ScoreRing 组、mini 条；
   Idea 2.0：深度 / 研究类型徽标。
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

/** 深度徽标：草案 / 研究方案。 */
export function DepthBadge({ depth }: { depth: IdeaDepth | undefined }) {
  if (!depth) return null;
  const proposal = depth === 'proposal';
  return (
    <span
      className="pill sm"
      style={{
        background: proposal ? 'var(--violet-bg)' : 'var(--surface-3)',
        color: proposal ? 'var(--violet-tx)' : 'var(--text-2)',
      }}
    >
      {proposal ? tr('研究方案', 'Proposal') : tr('草案', 'Sketch')}
    </span>
  );
}

/** 研究类型徽标（无类型时不渲染）。 */
export function ResearchTypeBadge({ type }: { type: string | null | undefined }) {
  if (!type) return null;
  return (
    <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
      {researchTypeLabel(type)}
    </span>
  );
}

export interface ScoreDim {
  key: keyof IdeaScores;
  zh: string;
  en: string;
}

export const SCORE_DIMS: ScoreDim[] = [
  { key: 'novelty', zh: '新颖', en: 'novelty' },
  { key: 'feasibility', zh: '可行', en: 'feasibility' },
  { key: 'operability', zh: '可操作', en: 'operability' },
  { key: 'impact', zh: '影响', en: 'impact' },
];

/** 四维均值 composite（无分数返回 null）。 */
export function compositeOf(scores: IdeaScores | null | undefined): number | null {
  if (!scores) return null;
  return (scores.novelty + scores.feasibility + scores.operability + scores.impact) / 4;
}

/** 四维 ScoreRing 组（候选卡用）。 */
export function ScoreRingGroup({ scores, size = 38 }: { scores: IdeaScores | null; size?: number }) {
  if (!scores) {
    return <span className="muted" style={{ fontSize: 12 }}>{tr('尚未打分', 'Not scored yet')}</span>;
  }
  return (
    <div className="row gap12 wrap">
      {SCORE_DIMS.map((d) => (
        <div key={d.key} className="col" style={{ alignItems: 'center', gap: 4 }}>
          <ScoreRing value={scores[d.key]} size={size} />
          <span style={{ fontSize: 10, color: 'var(--text-3)' }}>{tr(d.zh, d.en)}</span>
        </div>
      ))}
    </div>
  );
}

/** 单维横条（0-10，按阈值着色）。 */
export function RubricBar({ label, value }: { label: string; value: number }) {
  const color = value >= 7.5 ? 'var(--ok)' : value >= 6 ? 'var(--accent)' : 'var(--warn)';
  return (
    <div className="row gap10" style={{ marginBottom: 10 }}>
      <span style={{ fontSize: 12, width: 110, flexShrink: 0, color: 'var(--text-2)' }}>{label}</span>
      <div className="bar" style={{ flex: 1 }}>
        <i style={{ width: `${Math.max(0, Math.min(100, value * 10))}%`, background: color }} />
      </div>
      <span className="mono" style={{ fontSize: 12, fontWeight: 700, width: 30, textAlign: 'right' }}>
        {value.toFixed(1)}
      </span>
    </div>
  );
}

/** 排行榜里的四维 mini 条（一行四段）。 */
export function MiniScoreBars({ scores }: { scores: IdeaScores | null }) {
  if (!scores) return <span className="muted mono" style={{ fontSize: 11 }}>—</span>;
  return (
    <div className="row gap6" style={{ width: '100%' }}>
      {SCORE_DIMS.map((d) => {
        const v = scores[d.key];
        const color = v >= 7.5 ? 'var(--ok)' : v >= 6 ? 'var(--accent)' : 'var(--warn)';
        return (
          <div key={d.key} style={{ flex: 1 }} title={`${tr(d.zh, d.en)}: ${v.toFixed(1)}`}>
            <div className="bar" style={{ height: 5 }}>
              <i style={{ width: `${Math.max(0, Math.min(100, v * 10))}%`, background: color }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
