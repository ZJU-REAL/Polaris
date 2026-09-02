import { describe, expect, it } from 'vitest';
import {
  LITERATURE_SOURCES,
  RESOLVER_SOURCES,
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

  it('exposes Unpaywall as an OA resolver without treating it as a search source', () => {
    expect(RESOLVER_SOURCES.map((source) => source.id)).toContain('unpaywall');
    expect(validDraft().sources).not.toContain('unpaywall');
    expect(LITERATURE_SOURCES.find((source) => source.id === 'unpaywall')?.testQuery).toMatch(/^10\./);
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
    // 草稿里塞进掩码凭据和健康状态：这正是从 GET 拿回来的形状，直接回发就会把
    // "••••1234" 当成新密钥写进去。断言必须针对「草稿里有、载荷里没有」，
    // 否则用一份本来就不含这些字段的草稿去断言，等于什么都没测。
    const polluted = {
      ...validDraft(),
      provider_keys: { openalex: [{ id: 'x', preview: '••••1234', enabled: true }] },
      provider_health: { openalex: { status: 'ok' } },
    };
    const payload = buildLiteratureSettingsUpdate(polluted as never);
    expect(payload).toEqual(validDraft());
    expect(payload).not.toHaveProperty('provider_keys');
    expect(payload).not.toHaveProperty('provider_health');
    expect(JSON.stringify(payload)).not.toContain('••••1234');
  });
});
