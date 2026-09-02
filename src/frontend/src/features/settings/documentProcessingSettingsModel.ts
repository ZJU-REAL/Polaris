import type {
  DocumentProcessingSettings,
  DocumentProcessingSettingsUpdate,
} from '../../lib/api';

export type DocumentProcessingDraft = DocumentProcessingSettingsUpdate;

export function documentProcessingDraftFrom(
  settings: DocumentProcessingSettings,
): DocumentProcessingDraft {
  const { mineru_credentials: _credentials, ...draft } = settings;
  return draft;
}

export function validateDocumentProcessingDraft(
  draft: DocumentProcessingDraft,
): string | null {
  if (!draft.mineru_enabled && !draft.pymupdf_fallback_enabled) return 'parser_policy';
  try {
    const url = new URL(draft.mineru_base_url);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      return 'mineru_base_url';
    }
  } catch {
    return 'mineru_base_url';
  }
  if (!Number.isFinite(draft.mineru_timeout_seconds) || draft.mineru_timeout_seconds <= 30 || draft.mineru_timeout_seconds > 86_400) return 'mineru_timeout_seconds';
  if (!Number.isFinite(draft.mineru_poll_interval_seconds) || draft.mineru_poll_interval_seconds < 1 || draft.mineru_poll_interval_seconds > 300) return 'mineru_poll_interval_seconds';
  if (!Number.isInteger(draft.mineru_retries) || draft.mineru_retries < 0 || draft.mineru_retries > 5) return 'mineru_retries';
  if (!Number.isInteger(draft.mineru_concurrency) || draft.mineru_concurrency < 1 || draft.mineru_concurrency > 16) return 'mineru_concurrency';
  return null;
}
