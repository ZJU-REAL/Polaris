import type { Context } from '@deepseek-ai/cordis'
import type { SkillCandidate, SkillLookupOptions } from '@deepseek-ai/dsh-skill'
import { describe, expect, it, vi } from 'vitest'
import type { PolarisClient } from '../src/client.js'
import { PolarisApiError } from '../src/client.js'
import { PolarisSkillProvider } from '../src/provider.js'

const revision = 'b'.repeat(64)
const item = {
  id: '2b4e728f-28d9-4240-a826-65283f248b9d',
  slug: 'literature-review',
  name: 'Literature review',
  description: 'Use when reviewing a body of literature.',
  invocation: 'manual' as const,
  scope: 'user' as const,
  allowedTools: ['search_papers'],
  files: [{ path: 'rubric.md', size: 6, revision }],
  revision,
  updatedAt: '2026-08-13T12:00:00Z',
}

function context(): Context {
  return { logger: { warn: vi.fn() } } as unknown as Context
}

const options = {} as SkillLookupOptions

describe('PolarisSkillProvider', () => {
  it('publishes native candidates and loads their definition and policy', async () => {
    const client = {
      fetchCatalog: vi.fn().mockResolvedValue({
        kind: 'catalog',
        value: { revision, skills: [item] },
        etag: `"${revision}"`,
      }),
      fetchSkill: vi.fn().mockResolvedValue({ ...item, body: '# Review' }),
    } as unknown as PolarisClient
    const provider = new PolarisSkillProvider(
      context(), client, { user: 340, builtin: 360 }, 30_000,
    )

    const listed = await provider.list(options)
    expect(Array.isArray(listed)).toBe(true)
    const candidate = (listed as readonly SkillCandidate[])[0]
    expect(candidate).toMatchObject({
      name: 'literature-review',
      provider: 'polaris',
      rank: 340,
      invocation: { modelInvocable: false, userInvocable: true },
    })

    const definition = await provider.get(candidate!, options)
    expect(definition).toMatchObject({
      name: 'literature-review',
      provider: 'polaris',
      content: '# Review',
    })
    expect(provider.policy('literature-review')).toEqual({
      provider: 'polaris',
      name: 'literature-review',
      allowedTools: ['search_papers'],
      userInvocable: true,
      revision,
    })
  })

  it('fails closed and marks discovery incomplete after authentication loss', async () => {
    const client = {
      fetchCatalog: vi.fn()
        .mockResolvedValueOnce({
          kind: 'catalog',
          value: { revision, skills: [item] },
          etag: `"${revision}"`,
        })
        .mockRejectedValueOnce(new PolarisApiError(401, 'expired')),
    } as unknown as PolarisClient
    const provider = new PolarisSkillProvider(
      context(), client, { user: 340, builtin: 360 }, 30_000,
    )

    expect(await provider.list(options)).toHaveLength(1)
    await expect(provider.list(options)).resolves.toEqual({ candidates: [], complete: false })
    expect(provider.policy('literature-review')).toBeUndefined()
  })
})
