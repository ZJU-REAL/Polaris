import { afterEach, describe, expect, it, vi } from 'vitest'
import { PolarisApiError, PolarisClient } from '../src/client.js'

const revision = 'a'.repeat(64)

function catalog() {
  return {
    revision,
    skills: [{
      id: 'd95375e8-414d-467c-8928-8531dcbc864e',
      slug: 'paper-triage',
      name: 'Paper triage',
      description: 'Use when triaging papers.',
      invocation: 'auto',
      scope: 'user',
      allowedTools: ['search_papers'],
      files: [],
      revision,
      updatedAt: '2026-08-13T12:00:00Z',
    }],
  }
}

describe('PolarisClient', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('authenticates discovery and supports conditional requests', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(catalog()), {
        status: 200,
        headers: { 'content-type': 'application/json', etag: `"${revision}"` },
      }))
      .mockResolvedValueOnce(new Response(null, {
        status: 304,
        headers: { etag: `"${revision}"` },
      }))
    vi.stubGlobal('fetch', fetchMock)
    const client = new PolarisClient('https://polaris.example/base/', 'secret', 1000)

    const first = await client.fetchCatalog()
    expect(first.kind).toBe('catalog')
    const firstCall = fetchMock.mock.calls[0]
    expect(String(firstCall?.[0])).toBe(
      'https://polaris.example/base/api/integrations/deepseek-harness/v1/skills',
    )
    expect(firstCall?.[1]?.headers).toMatchObject({
      Authorization: 'Bearer secret',
      Accept: 'application/json',
    })

    const second = await client.fetchCatalog(undefined, `"${revision}"`)
    expect(second).toEqual({ kind: 'not-modified', etag: `"${revision}"` })
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toMatchObject({
      'If-None-Match': `"${revision}"`,
    })
  })

  it('encodes attachment path segments and reports HTTP failures', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response('template', {
        status: 200,
        headers: { etag: `"${revision}"` },
      }))
      .mockResolvedValueOnce(new Response('denied', { status: 403 }))
    vi.stubGlobal('fetch', fetchMock)
    const client = new PolarisClient('https://polaris.example', 'secret', 1000)

    await expect(client.fetchSkillFile('paper-triage', 'refs/a b.md')).resolves.toEqual({
      content: 'template',
      revision,
    })
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('refs/a%20b.md')
    await expect(client.fetchCatalog()).rejects.toEqual(
      expect.objectContaining<Partial<PolarisApiError>>({ status: 403 }),
    )
  })

  it('refuses dot-segment paths that would escape the skill-files route', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const client = new PolarisClient('https://polaris.example', 'secret', 1000)

    for (const path of ['../../api/integration-tokens', 'refs/../../secret', '.', 'refs//x.md']) {
      await expect(client.fetchSkillFile('paper-triage', path)).resolves.toBeUndefined()
    }
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
