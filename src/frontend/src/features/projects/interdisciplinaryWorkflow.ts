import type { InterdisciplinaryScopeDraft } from '../../lib/api';

export type ResearchMode = 'conventional' | 'interdisciplinary';

export function splitInterdisciplinaryTerms(value: string): string[] {
  return [...new Set(
    value
      .split(/[,，;；\n]/)
      .map((item) => item.trim())
      .filter(Boolean),
  )];
}

export function validateInterdisciplinaryScope(draft: InterdisciplinaryScopeDraft): string | null {
  if (draft.research_scope.trim().length < 10) return 'research_scope';
  if (!draft.core_questions.some((item) => item.trim())) return 'core_questions';
  if (draft.primary_domain.trim().length < 2) return 'primary_domain';
  if (!draft.related_domains.some((item) => item.trim())) return 'related_domains';
  return null;
}
