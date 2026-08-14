import type { Context } from '@deepseek-ai/cordis'
import type {
  SkillCandidate,
  SkillDefinition,
  SkillLookupOptions,
  SkillProvider,
  SkillProviderControl,
} from '@deepseek-ai/dsh-skill'
import type { PolarisSkillCatalogItem, PolarisSkillPolicy } from './contracts.js'
import { PolarisApiError, PolarisClient } from './client.js'

export interface ProviderRanks {
  readonly user: number
  readonly builtin: number
}

interface PolarisLocator {
  readonly id: string
  readonly slug: string
  readonly revision: string
}

function locator(value: unknown): PolarisLocator | undefined {
  if (typeof value !== 'object' || value === null) return undefined
  const record = value as Record<string, unknown>
  if (typeof record.id !== 'string'
    || typeof record.slug !== 'string'
    || typeof record.revision !== 'string') return undefined
  return { id: record.id, slug: record.slug, revision: record.revision }
}

export class PolarisSkillProvider implements SkillProvider {
  readonly name = 'polaris'
  private catalog: readonly PolarisSkillCatalogItem[] | undefined
  private etag: string | undefined
  private authoritative = false
  private readonly policies = new Map<string, PolarisSkillPolicy>()

  constructor(
    private readonly ctx: Context,
    private readonly client: PolarisClient,
    private readonly ranks: ProviderRanks,
    private readonly refreshIntervalMs: number,
  ) {}

  async initialize(signal?: AbortSignal): Promise<void> {
    await this.refresh(signal)
  }

  bind(control: SkillProviderControl): void {
    let timer: ReturnType<typeof setTimeout> | undefined
    const schedule = (): void => {
      if (control.signal.aborted) return
      timer = setTimeout(() => void poll(), this.refreshIntervalMs)
    }
    const poll = async (): Promise<void> => {
      try {
        const changed = await this.refresh(control.signal)
        if (changed) control.invalidate()
      } catch (error) {
        if (error instanceof PolarisApiError && [401, 403].includes(error.status)) {
          if (this.clear()) control.invalidate()
        }
        if (!control.signal.aborted) {
          this.ctx.logger.warn(`polaris-skill-provider: refresh failed: ${errorMessage(error)}`)
        }
      } finally {
        schedule()
      }
    }
    control.signal.addEventListener('abort', () => {
      if (timer !== undefined) clearTimeout(timer)
    }, { once: true })
    schedule()
  }

  readonly list = async (
    options: SkillLookupOptions,
  ): Promise<readonly SkillCandidate[] | { readonly candidates: readonly SkillCandidate[]; readonly complete: boolean }> => {
    try {
      await this.refresh(options.signal)
    } catch (error) {
      if (error instanceof PolarisApiError && [401, 403].includes(error.status)) {
        this.clear()
      }
      this.ctx.logger.warn(`polaris-skill-provider: discovery failed: ${errorMessage(error)}`)
      return { candidates: this.candidates(), complete: false }
    }
    const candidates = this.candidates()
    return this.authoritative ? candidates : { candidates, complete: false }
  }

  readonly get = async (
    candidate: SkillCandidate,
    options: SkillLookupOptions,
  ): Promise<SkillDefinition | undefined> => {
    const target = locator(candidate.locator)
    if (target === undefined) return undefined
    const definition = await this.client.fetchSkill(target.slug, options.signal)
    if (definition === undefined || definition.id !== target.id) {
      this.authoritative = false
      return undefined
    }
    this.rememberPolicy(definition)
    return {
      name: definition.slug,
      description: definition.description,
      invocation: {
        modelInvocable: definition.invocation === 'auto',
        userInvocable: true,
      },
      source: definition.scope === 'user' ? 'polaris-user' : 'polaris-builtin',
      provider: this.name,
      ...(definition.files.length === 0 ? {} : {
        resourceBase: {
          kind: 'opaque' as const,
          description: `Use polaris_skill_resource with skill="${definition.slug}" and the relative file path.`,
        },
      }),
      content: definition.body,
      metadata: {
        polarisAllowedTools: definition.allowedTools,
        polarisRevision: definition.revision,
      },
    }
  }

  policy(name: string): PolarisSkillPolicy | undefined {
    return this.policies.get(name)
  }

  private candidates(): readonly SkillCandidate[] {
    return (this.catalog ?? []).map(skill => ({
      name: skill.slug,
      description: skill.description,
      invocation: {
        modelInvocable: skill.invocation === 'auto',
        userInvocable: true,
      },
      source: skill.scope === 'user' ? 'polaris-user' : 'polaris-builtin',
      provider: this.name,
      rank: skill.scope === 'user' ? this.ranks.user : this.ranks.builtin,
      locator: { id: skill.id, slug: skill.slug, revision: skill.revision },
      ...(skill.files.length === 0 ? {} : {
        resourceBase: {
          kind: 'opaque' as const,
          description: `Use polaris_skill_resource with skill="${skill.slug}" and the relative file path.`,
        },
      }),
      metadata: {
        polarisAllowedTools: skill.allowedTools,
        polarisRevision: skill.revision,
      },
    }))
  }

  private async refresh(signal?: AbortSignal): Promise<boolean> {
    const result = await this.client.fetchCatalog(signal, this.etag)
    if (result.kind === 'not-modified') {
      this.authoritative = true
      if (result.etag !== undefined) this.etag = result.etag
      return false
    }
    const previousRevision = this.catalogRevision()
    this.catalog = result.value.skills
    this.etag = result.etag
    this.authoritative = true
    this.policies.clear()
    for (const skill of this.catalog) this.rememberPolicy(skill)
    return previousRevision !== result.value.revision
  }

  private catalogRevision(): string | undefined {
    const raw = this.etag?.replace(/^(?:W\/)?"|"$/g, '')
    return raw && /^[a-f0-9]{64}$/.test(raw) ? raw : undefined
  }

  private rememberPolicy(skill: PolarisSkillCatalogItem): void {
    this.policies.set(skill.slug, {
      provider: 'polaris',
      name: skill.slug,
      allowedTools: skill.allowedTools,
      userInvocable: true,
      revision: skill.revision,
    })
  }

  private clear(): boolean {
    const changed = this.catalog !== undefined || this.policies.size > 0
    this.catalog = undefined
    this.etag = undefined
    this.authoritative = false
    this.policies.clear()
    return changed
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
