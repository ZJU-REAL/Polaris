/**
 * 每日论文订阅：一份清单，arXiv 只是其中一个来源（不再单独占着「订阅分类」那张卡）。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { setLang } from '../../../lib/i18n';
import { addableSources, subscriptionsPayload, termIssue } from '../DailySubscriptionsSection';

beforeEach(() => setLang('en'));

describe('termIssue', () => {
  it('checks arXiv terms are category codes', () => {
    expect(termIssue('arxiv', 'cs.AI')).toBeNull();
    expect(termIssue('arxiv', 'q-bio.NC')).toBeNull();
    expect(termIssue('arxiv', 'hep-th')).toBeNull();
    expect(termIssue('arxiv', 'machine learning')).toMatch(/category/);
  });

  it('takes free search terms for other sources', () => {
    expect(termIssue('pubmed', 'machine learning')).toBeNull();
    expect(termIssue('pubmed', '   ')).not.toBeNull();
  });
});

describe('addableSources', () => {
  it('offers only daily-capable sources not already listed', () => {
    expect(addableSources(['arxiv', 'pubmed'], [{ source: 'pubmed' }])).toEqual(['arxiv']);
    expect(addableSources(['arxiv'], [{ source: 'arxiv' }])).toEqual([]);
  });
});

describe('subscriptionsPayload', () => {
  it('sends every source in one list and drops sources with no terms', () => {
    expect(
      subscriptionsPayload([
        { source: 'arxiv', terms: ['cs.AI'] },
        { source: 'pubmed', terms: [] },
        { source: 'europepmc', terms: ['glioma'] },
      ]),
    ).toEqual([
      { source: 'arxiv', terms: ['cs.AI'] },
      { source: 'europepmc', terms: ['glioma'] },
    ]);
  });
});

describe('daily settings layout', () => {
  it('has one subscriptions card instead of an arXiv card plus "other sources"', () => {
    const page = readFileSync(join(__dirname, '..', 'SettingsPage.tsx'), 'utf8');
    expect(page).toContain('<DailySubscriptionsSection />');
    expect(page).not.toContain('DailyCategoriesSection');
    expect(page).not.toContain('DailyOtherSourcesSection');
  });
});
