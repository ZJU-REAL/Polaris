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
  mineru_credentials: [],
};

describe('document-processing administrator settings', () => {
  it('omits write-only credentials from the settings update', () => {
    expect(documentProcessingDraftFrom(SETTINGS)).not.toHaveProperty('mineru_credentials');
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
