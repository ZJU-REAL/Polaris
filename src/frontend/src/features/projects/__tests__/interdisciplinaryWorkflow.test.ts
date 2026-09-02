import { describe, expect, it, vi } from 'vitest';
import type {
  InterdisciplinaryConfirmation,
  InterdisciplinaryScopeDraft,
  InterdisciplinaryScopeRead,
  ProjectRead,
} from '../../../lib/api';
import {
  createInterdisciplinaryProject,
  splitInterdisciplinaryTerms,
  validateInterdisciplinaryScope,
} from '../interdisciplinaryWorkflow';

const project: ProjectRead = {
  id: 'project-1',
  name: 'Impact-aware segmentation',
  slug: 'impact-aware-segmentation',
  statement: 'Combine structural impact mechanics with computer vision.',
  status: 'active',
  research_mode: 'interdisciplinary',
  owner_id: 'user-1',
  created_at: '2026-08-29T00:00:00Z',
  updated_at: '2026-08-29T00:00:00Z',
};

const scope: InterdisciplinaryScopeDraft = {
  research_scope: 'Study impact damage under controlled loading and image acquisition conditions.',
  core_questions: ['How can segmentation evidence quantify structural failure?'],
  primary_domain: 'Structural engineering',
  related_domains: ['Computer vision'],
};

const saved: InterdisciplinaryScopeRead = {
  ...scope,
  id: 'scope-1',
  project_id: project.id,
  version: 1,
  status: 'draft',
  created_by: 'user-1',
  confirmed_by: null,
  confirmed_at: null,
};

const confirmation: InterdisciplinaryConfirmation = {
  profile: { ...saved, status: 'confirmed', confirmed_by: 'user-1' },
  library_id: 'library-1',
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

  it('persists create, scope save and confirmation in order', async () => {
    const events: string[] = [];
    const client = {
      createProject: vi.fn(async () => { events.push('create'); return project; }),
      saveInterdisciplinaryScope: vi.fn(async () => { events.push('save'); return saved; }),
      confirmInterdisciplinaryScope: vi.fn(async () => { events.push('confirm'); return confirmation; }),
    };

    const result = await createInterdisciplinaryProject(client, {
      name: project.name,
      statement: project.statement!,
      sourceLibraryIds: ['discipline-library'],
      scope,
    });

    expect(events).toEqual(['create', 'save', 'confirm']);
    expect(client.createProject).toHaveBeenCalledWith({
      name: project.name,
      statement: project.statement,
      source_library_ids: ['discipline-library'],
      research_mode: 'interdisciplinary',
    });
    expect(result.confirmation.library_id).toBe('library-1');
  });

  it('reports a recoverable partial project when saving the scope fails', async () => {
    const client = {
      createProject: vi.fn(async () => project),
      saveInterdisciplinaryScope: vi.fn(async () => { throw new Error('save failed'); }),
      confirmInterdisciplinaryScope: vi.fn(async () => confirmation),
    };

    await expect(createInterdisciplinaryProject(client, {
      name: project.name,
      statement: project.statement!,
      sourceLibraryIds: [],
      scope,
    })).rejects.toMatchObject({
      stage: 'save-scope',
      project,
    });
    expect(client.confirmInterdisciplinaryScope).not.toHaveBeenCalled();
  });

  it('reports the confirmation stage without losing the created project', async () => {
    const client = {
      createProject: vi.fn(async () => project),
      saveInterdisciplinaryScope: vi.fn(async () => saved),
      confirmInterdisciplinaryScope: vi.fn(async () => { throw new Error('confirm failed'); }),
    };

    await expect(createInterdisciplinaryProject(client, {
      name: project.name,
      statement: project.statement!,
      sourceLibraryIds: [],
      scope,
    })).rejects.toMatchObject({
      stage: 'confirm-scope',
      project,
    });
  });
});
