/**
 * 「从哪里找文献」要真的到得了后端。
 *
 * 这一组断言防的是一类看界面发现不了的缺陷：来源选择器点得动、chip 亮得起来，
 * 但值没进 payload——建出来的库照旧只抓 arXiv，而用户以为自己选了 PubMed。
 * 这正是这轮文献库去 arXiv 化要消灭的「有能力没入口」。
 */
import { describe, expect, it } from 'vitest';
import {
  hasInclusionKeywords,
  keywordsFromInclusion,
  type InclusionValue,
} from '../InclusionSettingsForm';

function value(patch: Partial<InclusionValue> = {}): InclusionValue {
  return {
    sources: [],
    arxiv_categories: [],
    include: [],
    exclude: [],
    rubric: [],
    anchors: [],
    ...patch,
  };
}

describe('keywordsFromInclusion', () => {
  it('把选中的来源带进 payload', () => {
    expect(keywordsFromInclusion(value({ sources: ['pubmed', 'crossref'] })).sources).toEqual([
      'pubmed',
      'crossref',
    ]);
  });

  it('没选来源时给空数组，而不是丢掉这个字段', () => {
    // 缺字段和空数组在后端是同一个意思（只用 arXiv），但显式发出去
    // 才能覆盖掉库里存量的旧值——否则「从 PubMed 改回 arXiv」保存不下来
    expect(keywordsFromInclusion(value()).sources).toEqual([]);
  });

  it('来源与 arXiv 分类互不干扰', () => {
    const out = keywordsFromInclusion(value({ sources: ['arxiv'], arxiv_categories: ['cs.LG'] }));
    expect(out).toEqual({ sources: ['arxiv'], arxiv_categories: ['cs.LG'], include: [] });
  });
});

describe('hasInclusionKeywords', () => {
  it('只挑了来源也要下发', () => {
    // 建库时若只按这一项判空，用户挑的 PubMed 会被静默丢弃
    expect(hasInclusionKeywords(value({ sources: ['pubmed'] }))).toBe(true);
  });

  it('分类或关键词任一非空即下发', () => {
    expect(hasInclusionKeywords(value({ arxiv_categories: ['cs.LG'] }))).toBe(true);
    expect(hasInclusionKeywords(value({ include: ['catalysis'] }))).toBe(true);
  });

  it('三项全空则不下发', () => {
    expect(hasInclusionKeywords(value())).toBe(false);
    // 打分标准和锚点走各自的字段，不该把 keywords 顶成非空
    expect(hasInclusionKeywords(value({ anchors: [{ title: 'x' }] }))).toBe(false);
  });
});
