import { describe, expect, it } from 'vitest';
import {
  LITERATURE_SOURCES,
  buildLiteratureSettingsUpdate,
  type LiteratureSettingsDraft,
  validateLiteratureSettingsDraft,
} from '../literatureSettingsModel';

const validDraft = (): LiteratureSettingsDraft => ({
  sources: ['openalex', 'arxiv', 'pubmed'],
  requested_count: 50,
  candidate_budget: 200,
  start_year: 2016,
  end_year: 2026,
  score_weights: {
    relevance: 0.45,
    evidence_quality: 0.2,
    impact: 0.15,
    novelty: 0.1,
    recency: 0.1,
  },
});

describe('literature search settings model', () => {
  it('keeps arXiv keyless and exposes credential pools only where useful', () => {
    expect(LITERATURE_SOURCES.find((source) => source.id === 'arxiv')?.credentialMode).toBe('none');
    expect(LITERATURE_SOURCES.find((source) => source.id === 'openalex')?.credentialMode).toBe('optional');
    expect(LITERATURE_SOURCES.find((source) => source.id === 'sciverse')?.credentialMode).toBe('required');
    expect(LITERATURE_SOURCES.find((source) => source.id === 'easyscholar')?.metricOnly).toBe(true);
  });

  it('accepts the configured result count and year window', () => {
    expect(validateLiteratureSettingsDraft(validDraft())).toBeNull();
  });

  it('rejects a candidate budget smaller than the requested result count', () => {
    const draft = validDraft();
    draft.candidate_budget = 49;
    expect(validateLiteratureSettingsDraft(draft)).toBe('candidate_budget_lt_requested');
  });

  it('rejects inverted years and an empty source set', () => {
    const draft = validDraft();
    draft.start_year = 2027;
    expect(validateLiteratureSettingsDraft(draft)).toBe('year_window');
    draft.start_year = 2016;
    draft.sources = [];
    expect(validateLiteratureSettingsDraft(draft)).toBe('sources');
  });

  it('builds a settings-only payload without masked credentials or health state', () => {
    const payload = buildLiteratureSettingsUpdate(validDraft());
    expect(payload).toEqual(validDraft());
    expect(payload).not.toHaveProperty('provider_keys');
    expect(payload).not.toHaveProperty('provider_health');
  });
});
