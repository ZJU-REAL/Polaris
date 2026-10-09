/**
 * 「检索候选」这一步的小结要说真话：用了哪些源、从哪里来的（#821）。
 *
 * 以前检索那一支一律写「从 arXiv 检索到 N 篇」——选了 PubMed、OpenAlex 的库也这么显示；
 * 增量同步则靠 source === 'daily_feed' 识别，源一多就认不出来了。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { setLang } from '../../../lib/i18n';
import { wikiStepFriendly } from '../shared/StepCard';

beforeEach(() => setLang('en'));

describe('search step summary', () => {
  it('names the sources a build searched', () => {
    const out = wikiStepFriendly('wiki.search_candidates', {
      mode: 'bootstrap',
      source: 'pubmed,openalex',
      found: 12,
      inserted: 9,
    });
    expect(out?.text).toContain('PubMed, OpenAlex');
    expect(out?.text).not.toContain('arXiv');
  });

  it('still says arXiv for an arXiv library', () => {
    const out = wikiStepFriendly('wiki.search_candidates', { mode: 'bootstrap', source: 'arxiv', found: 3, inserted: 3 });
    expect(out?.text).toContain('on arXiv');
  });

  it('recognises an incremental sync from the pool by mode, not by an exact label', () => {
    const out = wikiStepFriendly('wiki.search_candidates', {
      mode: 'incremental',
      source: 'daily_feed(arxiv)',
      feed_total: 40,
      after_vector_rank: 20,
      already_in_library: 5,
      inserted: 4,
    });
    expect(out?.text).toContain('40 in the daily feed');
    expect(out?.text).toContain('4 new papers');
  });

  it('reports sources searched because they have no daily feed', () => {
    const out = wikiStepFriendly('wiki.search_candidates', {
      mode: 'incremental',
      source: 'openalex',
      searched_sources: ['openalex'],
      search_fetched: 10,
      feed_total: 0,
      inserted: 2,
    });
    expect(out?.text).toContain('10 from OpenAlex');
    expect(out?.text).not.toContain('daily feed');
  });
});
