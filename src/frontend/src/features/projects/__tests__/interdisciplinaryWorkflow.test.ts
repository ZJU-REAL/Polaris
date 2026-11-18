import { describe, expect, it } from 'vitest';
import type { InterdisciplinaryScopeDraft } from '../../../lib/api';
import { splitInterdisciplinaryTerms, validateInterdisciplinaryScope } from '../interdisciplinaryWorkflow';

const scope: InterdisciplinaryScopeDraft = {
  research_scope: 'Study impact damage under controlled loading and image acquisition conditions.',
  core_questions: ['How can segmentation evidence quantify structural failure?'],
  primary_domain: 'Structural engineering',
  related_domains: ['Computer vision'],
};

describe('interdisciplinary workflow', () => {
  it('normalizes and deduplicates comma-separated domains', () => {
    expect(splitInterdisciplinaryTerms('Mechanics， Computer vision; Mechanics\nStatistics')).toEqual([
      'Mechanics',
      'Computer vision',
      'Statistics',
    ]);
  });

  it('requires a usable scope, question, primary domain and related domain', () => {
    expect(validateInterdisciplinaryScope(scope)).toBeNull();
    expect(validateInterdisciplinaryScope({ ...scope, core_questions: [] })).toBe('core_questions');
    expect(validateInterdisciplinaryScope({ ...scope, related_domains: [] })).toBe('related_domains');
  });
});
