import {
  SkillCatalogSchema,
  SkillDefinitionSchema,
  type PolarisSkillCatalog,
  type PolarisSkillDefinition,
} from './contracts.js'

export class PolarisApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'PolarisApiError'
  }
}

export type CatalogFetchResult =
  | { readonly kind: 'not-modified'; readonly etag?: string }
  | { readonly kind: 'catalog'; readonly value: PolarisSkillCatalog; readonly etag?: string }

interface RequestOptions {
  readonly signal?: AbortSignal | undefined
  readonly etag?: string | undefined
  readonly accept?: string
}

export class PolarisClient {
  readonly baseUrl: URL

  constructor(
    baseUrl: string,
    private readonly token: string,
    private readonly timeoutMs: number,
  ) {
    this.baseUrl = new URL(baseUrl.endsWith('/') ? baseUrl : `${baseUrl}/`)
    if (!['http:', 'https:'].includes(this.baseUrl.protocol)) {
      throw new Error('baseUrl must use http or https')
    }
    if (token.length === 0) throw new Error('token must not be empty')
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1) {
      throw new Error('requestTimeoutMs must be a positive integer')
    }
  }

  async fetchCatalog(signal?: AbortSignal, etag?: string): Promise<CatalogFetchResult> {
    const response = await this.request(
      'api/integrations/deepseek-harness/v1/skills',
      { signal, etag },
    )
    const responseEtag = response.headers.get('etag') ?? undefined
    if (response.status === 304) {
      return { kind: 'not-modified', ...(responseEtag === undefined ? {} : { etag: responseEtag }) }
    }
    await this.requireOk(response)
    const value = SkillCatalogSchema.parse(await response.json())
    return {
      kind: 'catalog',
      value,
      ...(responseEtag === undefined ? {} : { etag: responseEtag }),
    }
  }

  async fetchSkill(slug: string, signal?: AbortSignal): Promise<PolarisSkillDefinition | undefined> {
    const response = await this.request(
      `api/integrations/deepseek-harness/v1/skills/${encodeURIComponent(slug)}`,
      { signal },
    )
    if (response.status === 404) return undefined
    await this.requireOk(response)
    return SkillDefinitionSchema.parse(await response.json())
  }

  async fetchSkillFile(
    slug: string,
    path: string,
    signal?: AbortSignal,
  ): Promise<{ readonly content: string; readonly revision?: string } | undefined> {
    const segments = path.split('/')
    // A skill declares attachments as fixed relative paths; encodeURIComponent
    // leaves '.'/'..' intact and new URL() collapses them, so an empty, dot, or
    // dot-dot segment would retarget the authenticated request outside the
    // skill-files route. Reject before encoding: an undeclared path is absent.
    if (segments.some(segment => segment === '' || segment === '.' || segment === '..')) {
      return undefined
    }
    const encodedPath = segments.map(segment => encodeURIComponent(segment)).join('/')
    const response = await this.request(
      `api/integrations/deepseek-harness/v1/skills/${encodeURIComponent(slug)}/files/${encodedPath}`,
      { signal, accept: 'text/plain' },
    )
    if (response.status === 404) return undefined
    await this.requireOk(response)
    const etag = response.headers.get('etag')?.replace(/^(?:W\/)?"|"$/g, '')
    return {
      content: await response.text(),
      ...(etag === undefined ? {} : { revision: etag }),
    }
  }

  private async request(path: string, options: RequestOptions): Promise<Response> {
    const controller = new AbortController()
    const timeout = setTimeout(
      () => controller.abort(new Error(`Polaris request timed out after ${this.timeoutMs}ms`)),
      this.timeoutMs,
    )
    const abort = (): void => controller.abort(options.signal?.reason)
    options.signal?.addEventListener('abort', abort, { once: true })
    if (options.signal?.aborted) abort()
    try {
      return await fetch(new URL(path, this.baseUrl), {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${this.token}`,
          Accept: options.accept ?? 'application/json',
          ...(options.etag === undefined ? {} : { 'If-None-Match': options.etag }),
        },
        signal: controller.signal,
      })
    } finally {
      clearTimeout(timeout)
      options.signal?.removeEventListener('abort', abort)
    }
  }

  private async requireOk(response: Response): Promise<void> {
    if (response.ok) return
    const detail = (await response.text()).slice(0, 500)
    throw new PolarisApiError(
      response.status,
      `Polaris API request failed with HTTP ${response.status}${detail ? `: ${detail}` : ''}`,
    )
  }
}
