import type {
  InterdisciplinaryConfirmation,
  InterdisciplinaryScopeDraft,
  InterdisciplinaryScopeRead,
  ProjectRead,
} from '../../lib/api';

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

export type InterdisciplinarySetupStage = 'save-scope' | 'confirm-scope';

export class InterdisciplinarySetupError extends Error {
  readonly project: ProjectRead;
  readonly stage: InterdisciplinarySetupStage;

  constructor(project: ProjectRead, stage: InterdisciplinarySetupStage, cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause));
    this.name = 'InterdisciplinarySetupError';
    this.project = project;
    this.stage = stage;
  }
}

interface WorkflowApi {
  createProject(input: {
    name: string;
    statement?: string;
    source_library_ids?: string[];
    research_mode?: ResearchMode;
  }): Promise<ProjectRead>;
  saveInterdisciplinaryScope(
    projectId: string,
    input: InterdisciplinaryScopeDraft,
  ): Promise<InterdisciplinaryScopeRead>;
  confirmInterdisciplinaryScope(projectId: string): Promise<InterdisciplinaryConfirmation>;
}

export async function createInterdisciplinaryProject(
  client: WorkflowApi,
  input: {
    name: string;
    statement: string;
    sourceLibraryIds: string[];
    scope: InterdisciplinaryScopeDraft;
  },
): Promise<{ project: ProjectRead; confirmation: InterdisciplinaryConfirmation }> {
  const project = await client.createProject({
    name: input.name,
    statement: input.statement,
    source_library_ids: input.sourceLibraryIds,
    research_mode: 'interdisciplinary',
  });

  try {
    await client.saveInterdisciplinaryScope(project.id, input.scope);
  } catch (error) {
    throw new InterdisciplinarySetupError(project, 'save-scope', error);
  }

  try {
    const confirmation = await client.confirmInterdisciplinaryScope(project.id);
    return { project, confirmation };
  } catch (error) {
    throw new InterdisciplinarySetupError(project, 'confirm-scope', error);
  }
}
