import type { IdeaDetail, IdeaEvidence, IdeaRead, IdeaScores, LeaderboardRow } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   想法页（列表 + 详情）共用的几样纯计算：名次、来历、分数的写法，
   以及把想法正文拆成「尚无依据的论断」和其余部分。
   ============================================================ */

export interface IdeaRank {
  /** 第几名（并列的名次相同） */
  rank: number;
  /** 同一名次不止一个 */
  tied: boolean;
  /** 对比过几场、赢了几场 */
  matches: number;
  wins: number;
  /** 参加过对比的想法一共多少个 */
  of: number;
}

/**
 * 两两对比后的名次。只给对比过的想法排（没对比过的没有名次）；
 * 评分相同的并列。输入是排行接口的结果（已按评分从高到低）。
 */
export function rankIdeas(
  rows: Pick<LeaderboardRow, 'id' | 'elo_rating' | 'matches' | 'wins' | 'rank' | 'rank_shared_by' | 'tied'>[],
): Map<string, IdeaRank> {
  const played = rows.filter((r) => r.matches > 0).sort((a, b) => b.elo_rating - a.elo_rating);
  const out = new Map<string, IdeaRank>();
  // 引擎给了名次就用引擎的（它知道并列）；旧引擎不给，下面自己按评分算
  if (played.length > 0 && played.every((r) => typeof r.rank === 'number')) {
    for (const r of played) {
      const shared = r.rank_shared_by ?? 1;
      out.set(r.id, { rank: r.rank as number, tied: r.tied ?? shared > 1, matches: r.matches, wins: r.wins, of: played.length });
    }
    return out;
  }
  let rank = 0;
  let prev: number | null = null;
  played.forEach((r, i) => {
    const rating = Math.round(r.elo_rating);
    if (prev === null || rating !== prev) rank = i + 1;
    prev = rating;
    out.set(r.id, { rank, tied: false, matches: r.matches, wins: r.wins, of: played.length });
  });
  const counts = new Map<number, number>();
  for (const v of out.values()) counts.set(v.rank, (counts.get(v.rank) ?? 0) + 1);
  for (const v of out.values()) v.tied = (counts.get(v.rank) ?? 0) > 1;
  return out;
}

/** 「第 2 名」/「并列第 1」。 */
export function rankText(r: IdeaRank): string {
  return r.tied ? tr(`并列第 ${r.rank}`, `Tied #${r.rank}`) : tr(`第 ${r.rank} 名`, `#${r.rank}`);
}

/** 「2 胜 1 负」。 */
export function recordText(r: Pick<IdeaRank, 'matches' | 'wins'>): string {
  const lost = Math.max(0, r.matches - r.wins);
  return tr(`${r.wins} 胜 ${lost} 负`, `${r.wins} won, ${lost} lost`);
}

/**
 * 卡片和详情页头上的那句名次。名次不并列时写「第 2 名 · 2 胜 1 负」；
 * 并列时名次本身说明不了什么（只比过一轮时半数想法都「并列第 1」），只写胜负。
 */
export function standingText(r: IdeaRank): string {
  return r.tied ? tr(`对比 ${recordText(r)}`, `Compared: ${recordText(r)}`) : `${rankText(r)} · ${recordText(r)}`;
}

export type IdeaOriginKind = 'thorough' | 'quick' | 'proposal' | 'manual';

/** 这个想法是怎么来的。和「再想一些」里的两种方式用同样的叫法。 */
export function originKind(idea: Pick<IdeaRead, 'depth' | 'origin' | 'source_voyage_id'>): IdeaOriginKind {
  if (idea.depth === 'proposal') return 'proposal';
  if (idea.origin?.kind === 'discovery') return 'thorough';
  // 引擎记了是哪个任务写的（source_voyage_id）：不带 origin 的就是快速生成。
  // 没记任务的是手工写入的，或旧引擎生成的（那时不记）——不猜，不贴标签。
  return idea.source_voyage_id ? 'quick' : 'manual';
}

export function originLabel(kind: IdeaOriginKind): string {
  switch (kind) {
    case 'thorough':
      return tr('深入生成', 'Thorough');
    case 'quick':
      return tr('快速生成', 'Quick');
    case 'proposal':
      return tr('研究方案', 'Proposal');
    default:
      return '';
  }
}

/** 来历的一句说明（详情页用）。 */
export function originSentence(kind: IdeaOriginKind): string {
  switch (kind) {
    case 'thorough':
      return tr('深入生成：先提出假设，再逐条对照文献核对', 'Thorough: proposed as a hypothesis, then checked against the papers');
    case 'quick':
      return tr('快速生成：直接从已读论文里提出，没有逐条核对', 'Quick: proposed straight from the papers, not checked claim by claim');
    case 'proposal':
      return tr('研究方案', 'Proposal');
    default:
      return '';
  }
}

/** 列表里的三项分数，一行安静的字（和课题时间线上的写法一致）。 */
export function scoresLine(s: IdeaScores | null | undefined): string {
  if (!s) return '';
  const n = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));
  return [
    tr(`新颖性 ${n(s.novelty)}`, `Novelty ${n(s.novelty)}`),
    tr(`可行性 ${n(s.feasibility)}`, `Feasibility ${n(s.feasibility)}`),
    tr(`影响 ${n(s.impact)}`, `Impact ${n(s.impact)}`),
  ].join(' · ');
}

/** 四项分数的平均；没打分返回 null。 */
export function overallScore(s: IdeaScores | null | undefined): number | null {
  if (!s) return null;
  return (s.novelty + s.feasibility + s.operability + s.impact) / 4;
}

export interface EvidenceCounts {
  support: number;
  refute: number;
  unsupported: number;
}

/** 从详情算出「几篇支持 / 几篇反对 / 几处尚无依据」。 */
export function evidenceCountsOf(idea: Pick<IdeaDetail, 'evidence' | 'origin'>): EvidenceCounts {
  const ev = Array.isArray(idea.evidence) ? idea.evidence : [];
  const refute = ev.filter((e) => e.stance === 'refute').length;
  return {
    support: ev.length - refute,
    refute,
    unsupported: unsupportedOf(idea).length,
  };
}

export function unsupportedOf(idea: Pick<IdeaRead, 'origin'>): string[] {
  const list = idea.origin?.unsupported_subclaims;
  return Array.isArray(list) ? list.filter((s): s is string => typeof s === 'string' && s.trim() !== '') : [];
}

/** 依据按立场分组：支持 / 反对。没写立场的算支持（它是作为依据被引的）。 */
export function groupEvidence(evidence: IdeaEvidence[] | null | undefined): { support: IdeaEvidence[]; refute: IdeaEvidence[] } {
  const list = Array.isArray(evidence) ? evidence : [];
  return {
    support: list.filter((e) => e.stance !== 'refute'),
    refute: list.filter((e) => e.stance === 'refute'),
  };
}

// 真实模型写的标题带尾巴：「尚无文献依据的部分」「尚无依据的论断」「Not yet supported by the literature:」
const UNSUPPORTED_HEADING =
  /^#{1,6}\s*(not yet supported(?: by the (?:literature|papers))?|unsupported (?:claims|parts)|尚无文献依据|尚无依据|文献尚未支持|尚未被文献支持)(?:的(?:部分|论断|内容|主张))?\s*[:：]?\s*$/i;

/**
 * 正文里「尚无文献依据」那一节单独拿出来：详情页把它放在依据后面，
 * 正文里就不再重复一遍。没有这一节时原样返回。
 */
export function splitUnsupportedSection(content: string): { body: string; unsupported: string[] } {
  if (!content) return { body: content, unsupported: [] };
  const lines = content.split('\n');
  const start = lines.findIndex((l) => UNSUPPORTED_HEADING.test(l.trim()));
  if (start < 0) return { body: content, unsupported: [] };
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i += 1) {
    if (/^#{1,6}\s/.test(lines[i]!)) {
      end = i;
      break;
    }
  }
  const section = lines.slice(start + 1, end);
  const unsupported = section
    .map((l) => l.trim())
    .filter((l) => /^[-*+]\s+/.test(l))
    .map((l) => l.replace(/^[-*+]\s+/, '').trim())
    .filter(Boolean);
  const body = [...lines.slice(0, start), ...lines.slice(end)].join('\n').replace(/\n{3,}/g, '\n\n').trimEnd();
  return { body, unsupported };
}

const SECTION_NAMES: Record<string, { zh: string; en: string }> = {
  motivation: { zh: '动机', en: 'Motivation' },
  method: { zh: '方法', en: 'Method' },
  experiments: { zh: '实验', en: 'Experiments' },
  risks: { zh: '风险', en: 'Risks' },
};

/**
 * 模型写的小节标题有时是固定的英文（Motivation / Method / Experiments / Risks）。
 * 中文界面下换成中文，别的标题不动。
 */
export function localizeHeadings(content: string): string {
  if (!content) return content;
  return content
    .split('\n')
    .map((line) => {
      const m = /^(#{1,6})\s*([A-Za-z]+)\s*$/.exec(line);
      if (!m) return line;
      const name = SECTION_NAMES[m[2]!.toLowerCase()];
      return name ? `${m[1]} ${tr(name.zh, name.en)}` : line;
    })
    .join('\n');
}
