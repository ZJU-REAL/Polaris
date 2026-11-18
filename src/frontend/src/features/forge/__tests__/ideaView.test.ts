import { describe, expect, it } from 'vitest';
import { setLang } from '../../../lib/i18n';
import { comparePlan } from '../CompareModal';
import { matchInfo } from '../IdeaComparisons';
import {
  evidenceCountsOf,
  groupEvidence,
  localizeHeadings,
  originKind,
  rankIdeas,
  splitUnsupportedSection,
  standingText,
} from '../ideaView';

const row = (id: string, elo: number, matches: number, wins: number) => ({ id, elo_rating: elo, matches, wins });

describe('rankIdeas', () => {
  it('ranks only compared ideas and marks ties', () => {
    const ranks = rankIdeas([row('a', 1232, 2, 2), row('b', 1216, 1, 1), row('c', 1216, 1, 1), row('d', 1200, 0, 0), row('e', 1184, 1, 0)]);
    expect(ranks.has('d')).toBe(false);
    expect(ranks.get('a')).toMatchObject({ rank: 1, tied: false, of: 4 });
    expect(ranks.get('b')).toMatchObject({ rank: 2, tied: true });
    expect(ranks.get('c')).toMatchObject({ rank: 2, tied: true });
    expect(ranks.get('e')).toMatchObject({ rank: 4, tied: false });
  });

  it('writes a tie as a win/loss record instead of a shared rank', () => {
    setLang('zh');
    const ranks = rankIdeas([row('a', 1216, 1, 1), row('b', 1216, 1, 1), row('c', 1184, 3, 1)]);
    expect(standingText(ranks.get('a')!)).toBe('对比 1 胜 0 负');
    expect(standingText(ranks.get('c')!)).toBe('第 3 名 · 1 胜 2 负');
    setLang('en');
    expect(standingText(ranks.get('a')!)).toBe('Compared: 1 won, 0 lost');
    setLang('zh');
  });
});

describe('where an idea came from', () => {
  it('tells thorough, quick and proposal apart', () => {
    expect(originKind({ depth: 'sketch', origin: { kind: 'discovery' }, source_voyage_id: 'v' })).toBe('thorough');
    expect(originKind({ depth: 'sketch', origin: null, source_voyage_id: null })).toBe('quick');
    expect(originKind({ depth: 'proposal', origin: null, source_voyage_id: null })).toBe('proposal');
  });
});

describe('evidence', () => {
  const ev = (stance: string | undefined, paper: string) => ({ paper_id: paper, title: paper, url: null, why: 'w', source: 'library' as const, stance });

  it('groups by stance and counts unsupported claims', () => {
    const idea = { evidence: [ev('support', 'p1'), ev('refute', 'p2'), ev(undefined, 'p3')], origin: { kind: 'discovery', unsupported_subclaims: ['x', ''] } };
    expect(groupEvidence(idea.evidence).support.map((e) => e.paper_id)).toEqual(['p1', 'p3']);
    expect(groupEvidence(idea.evidence).refute.map((e) => e.paper_id)).toEqual(['p2']);
    expect(evidenceCountsOf(idea)).toEqual({ support: 2, refute: 1, unsupported: 1 });
  });

  it('survives missing evidence', () => {
    expect(evidenceCountsOf({ evidence: null, origin: null })).toEqual({ support: 0, refute: 0, unsupported: 0 });
  });
});

describe('idea body', () => {
  it('pulls the unsupported section out of the body', () => {
    const src = '## Method\n\nDo it.\n\n## Not yet supported by the literature\n\n- claim one\n- claim two\n\n## Risks\n\nSome.';
    const out = splitUnsupportedSection(src);
    expect(out.unsupported).toEqual(['claim one', 'claim two']);
    expect(out.body).toBe('## Method\n\nDo it.\n\n## Risks\n\nSome.');
  });

  it('leaves a body without that section alone', () => {
    const src = '## Method\n\nDo it.';
    expect(splitUnsupportedSection(src)).toEqual({ body: src, unsupported: [] });
  });

  it('localizes the fixed English section names only', () => {
    setLang('zh');
    expect(localizeHeadings('## Motivation\n\ntext\n\n## Method\n\n## Something else')).toBe('## 动机\n\ntext\n\n## 方法\n\n## Something else');
    setLang('en');
    expect(localizeHeadings('## Motivation')).toBe('## Motivation');
    setLang('zh');
  });
});

describe('comparisons', () => {
  it('pairs sketches with sketches and proposals with proposals, leaving rejected ideas out', () => {
    const ideas = [
      { id: '1', status: 'candidate' as const, depth: 'sketch' as const },
      { id: '2', status: 'promoted' as const, depth: 'sketch' as const },
      { id: '3', status: 'under_review' as const, depth: 'sketch' as const },
      { id: '4', status: 'rejected' as const, depth: 'sketch' as const },
      { id: '5', status: 'candidate' as const, depth: 'proposal' as const },
    ];
    expect(comparePlan(ideas)).toEqual({ ids: ['1', '2', '3', '5'], matches: 1 });
  });

  it('reads the opponent and the result from a comparison record', () => {
    const session = { id: 's', target_type: 'idea_match', target_id: 'a', status: 'done', created_at: '', payload: { idea_a: 'a', idea_b: 'b', winner: 'b' } };
    expect(matchInfo(session, 'a')).toMatchObject({ opponentId: 'b', decided: true, won: false });
    expect(matchInfo(session, 'b')).toMatchObject({ opponentId: 'a', decided: true, won: true });
    expect(matchInfo({ ...session, payload: { idea_a: 'a', idea_b: 'b' } }, 'a')).toMatchObject({ decided: false, won: false });
  });
});
