import { describe, expect, it } from 'vitest';
import type { DocumentProcessingSettings } from '../../../lib/api';
import {
  documentProcessingDraftFrom,
  validateDocumentProcessingDraft,
} from '../documentProcessingSettingsModel';

const SETTINGS: DocumentProcessingSettings = {
  mineru_enabled: true,
  mineru_base_url: 'https://mineru.net/api/v4',
  mineru_timeout_seconds: 3600,
  mineru_poll_interval_seconds: 10,
  mineru_retries: 2,
  mineru_concurrency: 2,
  pymupdf_fallback_enabled: true,
  mineru_credentials: [
    {
      id: 'c1',
      provider: 'mineru',
      index: 0,
      configured: true,
      preview: '••••1234',
      enabled: true,
      label: null,
      health: null,
      created_at: null,
      updated_at: null,
    },
  ],
};

describe('document-processing administrator settings', () => {
  it('omits write-only credentials from the settings update', () => {
    // fixture 里必须真的带着凭据，否则 not.toHaveProperty 是空转的：
    // 用一份本来就没有该字段的输入去断言它不存在，删掉排除逻辑也不会红。
    const draft = documentProcessingDraftFrom(SETTINGS);
    expect(draft).not.toHaveProperty('mineru_credentials');
    expect(JSON.stringify(draft)).not.toContain('••••1234');
  });

  it('accepts the production MinerU policy', () => {
    expect(validateDocumentProcessingDraft(documentProcessingDraftFrom(SETTINGS))).toBeNull();
  });

  it('requires one parser and validates worker bounds', () => {
    expect(validateDocumentProcessingDraft({
      ...documentProcessingDraftFrom(SETTINGS),
      mineru_enabled: false,
      pymupdf_fallback_enabled: false,
    })).toBe('parser_policy');
    expect(validateDocumentProcessingDraft({
      ...documentProcessingDraftFrom(SETTINGS),
      mineru_concurrency: 17,
    })).toBe('mineru_concurrency');
  });

  it('rejects API roots containing credentials or query parameters', () => {
    expect(validateDocumentProcessingDraft({
      ...documentProcessingDraftFrom(SETTINGS),
      mineru_base_url: 'https://token@mineru.net/api?key=secret',
    })).toBe('mineru_base_url');
  });
});
