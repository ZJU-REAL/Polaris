/* 文献源适配器（#646）的单元测试：全程不打真网——fetch 一律注入
   fixture 替身。CORDIS fixture 按真实 API 响应裁剪（2026-09 对
   cordis.europa.eu 实测的形状，见 cordis-projects.ts 文件头），
   OpenAlex fixture 按官方 work 对象形状（与后端 openalex.py 同源）。 */
import { describe, expect, it } from 'vitest'
import {
  CORDIS_BASE,
  CordisProjectsAdapter,
  OpenAlexAdapter,
  buildCordisQuery,
  cordisDetailRecord,
  cordisSearchRecord,
  createKernel,
  openAlexRecord,
  sources,
  type FetchImpl,
  type SourcesService,
} from '../src/index.ts'

/* ---------- fetch 替身：记录请求，按 URL 回 fixture ---------- */

interface FakeCall {
  url: URL
  headers: Record<string, string>
}

function fakeFetch(handler: (url: URL) => { status?: number; body?: unknown }): {
  impl: FetchImpl
  calls: FakeCall[]
} {
  const calls: FakeCall[] = []
  const impl = (async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input))
    calls.push({ url, headers: { ...((init?.headers as Record<string, string>) ?? {}) } })
    const { status = 200, body = {} } = handler(url)
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })
  }) as FetchImpl
  return { impl, calls }
}

/* ---------- OpenAlex fixtures ---------- */

const openAlexWork = {
  id: 'https://openalex.org/W2741809807',
  title: 'Graphene at wafer scale',
  publication_year: 2021,
  publication_date: '2021-03-01',
  doi: 'https://doi.org/10.48550/arXiv.2103.01234',
  cited_by_count: 42,
  abstract_inverted_index: { Graphene: [0], scales: [1], well: [2] },
  primary_location: {
    landing_page_url: 'https://arxiv.org/abs/2103.01234',
    source: { display_name: 'arXiv' },
  },
  authorships: [
    {
      author: { display_name: 'Ada Lovelace' },
      // 重复机构必须去重保序
      institutions: [{ display_name: 'Zhejiang University' }, { display_name: 'Zhejiang University' }],
    },
    { author: { display_name: 'Alan Turing' }, institutions: [] },
    // 没有名字的 authorship 必须被丢弃
    { author: {}, institutions: [{ display_name: 'Ghost Lab' }] },
  ],
}

describe('openAlexRecord mapping', () => {
  it('normalizes a work into a SourceRecord aligned with backend paper fields', () => {
    const record = openAlexRecord(openAlexWork)
    expect(record.id).toBe('W2741809807')
    expect(record.source).toBe('openalex')
    expect(record.title).toBe('Graphene at wafer scale')
    expect(record.year).toBe(2021)
    expect(record.venue).toBe('arXiv')
    expect(record.doi).toBe('10.48550/arXiv.2103.01234')
    // DataCite DOI 前缀反推 arXiv id（与后端 ARXIV_DOI_TEMPLATE 同款约定）
    expect(record.arxivId).toBe('2103.01234')
    expect(record.abstract).toBe('Graphene scales well')
    expect(record.url).toBe('https://arxiv.org/abs/2103.01234')
    expect(record.authors).toEqual([
      { name: 'Ada Lovelace', affiliations: ['Zhejiang University'] },
      { name: 'Alan Turing' },
    ])
    expect(record.extra).toMatchObject({ citedByCount: 42, publishedDate: '2021-03-01' })
  })

  it('leaves optional fields absent when the work has no doi/abstract/url', () => {
    const record = openAlexRecord({ id: 'https://openalex.org/W1', title: 'Bare' })
    expect(record.year).toBeNull()
    expect(record.venue).toBeNull()
    expect(record.doi).toBeUndefined()
    expect(record.arxivId).toBeUndefined()
    expect(record.abstract).toBeUndefined()
    expect(record.url).toBeUndefined()
  })
})

describe('OpenAlexAdapter', () => {
  it('searches /works with per-page, mailto and an identifying UA', async () => {
    const { impl, calls } = fakeFetch(() => ({ body: { results: [openAlexWork, null] } }))
    const adapter = new OpenAlexAdapter({ mailto: 'team@example.com', fetchImpl: impl })
    const records = await adapter.search('graphene wafers', { limit: 3 })

    expect(records).toHaveLength(1)
    expect(records[0]!.id).toBe('W2741809807')
    const call = calls[0]!
    expect(call.url.origin).toBe('https://api.openalex.org')
    expect(call.url.pathname).toBe('/works')
    expect(call.url.searchParams.get('search')).toBe('graphene wafers')
    expect(call.url.searchParams.get('per-page')).toBe('3')
    expect(call.url.searchParams.get('mailto')).toBe('team@example.com')
    expect(call.headers['user-agent']).toContain('polaris-kernel')
    expect(call.headers['user-agent']).toContain('team@example.com')
  })

  it('fetchByIds hits /works/{id}, skips 404s and keeps the rest', async () => {
    const { impl, calls } = fakeFetch((url) =>
      url.pathname.endsWith('/W404') ? { status: 404 } : { body: openAlexWork },
    )
    const adapter = new OpenAlexAdapter({ fetchImpl: impl })
    const records = await adapter.fetchByIds(['W404', 'W2741809807'])

    expect(records.map((r) => r.id)).toEqual(['W2741809807'])
    expect(calls.map((c) => c.url.pathname)).toEqual(['/works/W404', '/works/W2741809807'])
  })

  it('surfaces non-404 failures instead of returning partial silence', async () => {
    const { impl } = fakeFetch(() => ({ status: 503 }))
    const adapter = new OpenAlexAdapter({ fetchImpl: impl })
    await expect(adapter.search('x')).rejects.toThrow(/HTTP 503/)
    await expect(adapter.fetchByIds(['W1'])).rejects.toThrow(/HTTP 503/)
  })
})

/* ---------- CORDIS fixtures（按真实响应裁剪） ---------- */

const cordisSearchHit = {
  reference: '811715',
  id: '811715',
  acronym: 'G4SEMI',
  programme: [
    { code: 'H2020', id: 'H2020-EU.3.', title: "PRIORITY 'Societal challenges" },
    { code: 'H2020', id: 'H2020-EU.2.3.', title: 'INDUSTRIAL LEADERSHIP - Innovation In SMEs' },
  ],
  // 列表端点的日期是本地化模板串——年份只能正则抠
  startDate: '1 {{month_05}} 2018',
  endDate: '30 {{month_04}} 2020',
  coordinatedIn: 'Spain',
  teaser: 'G4SEMI goal is to create added value through Graphene-on-wafer...',
  rcn: '217599',
  contentType: 'project',
  title: 'Graphene for Semiconductor Industry',
}

const cordisDetail = {
  contenttype: 'project',
  rcn: '217599',
  id: '811715',
  acronym: 'G4SEMI',
  teaser: 'G4SEMI goal is to create added value...',
  objective: 'G4SEMI goal is to create added value through the introduction of Graphene-on-wafer at competitive cost.',
  title: 'Graphene for Semiconductor Industry',
  keywords: 'Graphene, CVD Graphene, photosensors',
  totalCost: '1961125',
  ecMaxContribution: '1372787.5',
  startDate: '2018-05-01',
  endDate: '2020-04-30',
  status: 'CLOSED',
  identifiers: { grantDoi: '10.3030/811715' },
  relations: {
    associations: [
      {
        contenttype: 'organization',
        legalName: 'GRAPHENEA SEMICONDUCTOR SL',
        attributes: { type: 'coordinator', ecContribution: '1372787.5' },
      },
      { contenttype: 'organization', legalName: 'SOME THIRD PARTY', attributes: { type: 'thirdParty' } },
      {
        contenttype: 'programme',
        code: 'H2020-EU.2.1.',
        frameworkProgramme: 'H2020',
        title: 'INDUSTRIAL LEADERSHIP - LEIT',
        attributes: { type: 'relatedLegalBasis' },
      },
      {
        contenttype: 'programme',
        code: 'EIC-SMEInst-2018-2020',
        frameworkProgramme: 'H2020',
        title: 'SME instrument',
        attributes: { type: 'relatedTopic' },
      },
    ],
  },
}

describe('cordis query + mapping', () => {
  it('builds the documented query language and strips quote characters', () => {
    expect(buildCordisQuery('graphene wafers')).toBe("contenttype='project' AND 'graphene wafers'")
    expect(buildCordisQuery("gra'phene   chips")).toBe("contenttype='project' AND 'gra phene chips'")
  })

  it('maps a search hit: teaser→abstract, year from localized date, programme→venue/extra', () => {
    const record = cordisSearchRecord(cordisSearchHit)
    expect(record.id).toBe('811715')
    expect(record.source).toBe('cordis-projects')
    expect(record.title).toBe('Graphene for Semiconductor Industry')
    expect(record.authors).toEqual([]) // 列表响应没有协调机构名
    expect(record.year).toBe(2018)
    expect(record.venue).toBe('H2020')
    expect(record.abstract).toContain('Graphene-on-wafer')
    expect(record.url).toBe(`${CORDIS_BASE}/project/id/811715`)
    expect(record.extra).toMatchObject({ acronym: 'G4SEMI', rcn: '217599', coordinatedIn: 'Spain' })
    expect((record.extra as { programme: unknown[] }).programme).toHaveLength(2)
  })

  it('maps a detail: objective→abstract, coordinator→authors, grantDoi→doi, funding in extra', () => {
    const record = cordisDetailRecord(cordisDetail)
    expect(record.id).toBe('811715')
    expect(record.abstract).toBe(cordisDetail.objective)
    expect(record.authors).toEqual([{ name: 'GRAPHENEA SEMICONDUCTOR SL' }])
    expect(record.doi).toBe('10.3030/811715')
    expect(record.year).toBe(2018)
    expect(record.venue).toBe('H2020')
    expect(record.extra).toMatchObject({
      funding: { totalCost: '1961125', ecMaxContribution: '1372787.5' },
      keywords: 'Graphene, CVD Graphene, photosensors',
      status: 'CLOSED',
    })
    // programme 只收 relatedLegalBasis，relatedTopic 不混进来
    expect((record.extra as { programme: { code: string | null }[] }).programme).toEqual([
      { code: 'H2020-EU.2.1.', title: 'INDUSTRIAL LEADERSHIP - LEIT' },
    ])
  })
})

describe('CordisProjectsAdapter', () => {
  it('searches /api/search/results with the query language and json format', async () => {
    const { impl, calls } = fakeFetch(() => ({
      body: { status: true, payload: { total: 1, results: [cordisSearchHit] } },
    }))
    const adapter = new CordisProjectsAdapter({ fetchImpl: impl })
    const records = await adapter.search('graphene', { limit: 5 })

    expect(records.map((r) => r.id)).toEqual(['811715'])
    const call = calls[0]!
    expect(call.url.origin).toBe('https://cordis.europa.eu')
    expect(call.url.pathname).toBe('/api/search/results')
    expect(call.url.searchParams.get('q')).toBe("contenttype='project' AND 'graphene'")
    expect(call.url.searchParams.get('format')).toBe('json')
    expect(call.url.searchParams.get('num')).toBe('5')
    expect(call.url.searchParams.get('page')).toBe('1')
  })

  it('fetchByIds hits /project/id/{id}?format=json and skips 404s', async () => {
    const { impl, calls } = fakeFetch((url) =>
      url.pathname.endsWith('/999999') ? { status: 404 } : { body: cordisDetail },
    )
    const adapter = new CordisProjectsAdapter({ fetchImpl: impl })
    const records = await adapter.fetchByIds(['999999', '811715'])

    expect(records.map((r) => r.id)).toEqual(['811715'])
    expect(calls.map((c) => c.url.pathname)).toEqual(['/project/id/999999', '/project/id/811715'])
    expect(calls[1]!.url.searchParams.get('format')).toBe('json')
  })
})

/* ---------- sources 插件：注册 / 开关 / 卸载回收 ---------- */

describe('sources plugin', () => {
  it('registers enabled adapters and filters disabled ones', async () => {
    const kernel = createKernel({ name: 'sources-test' })
    await kernel.start()
    await kernel.ctx.plugin(sources, {
      adapters: {
        openalex: { enabled: true, mailto: 'team@example.com' },
        'cordis-projects': { enabled: false },
      },
    })

    const service = kernel.ctx.get('sources') as SourcesService
    expect(service).toBeTruthy()
    expect(service.list().map((a) => a.id)).toEqual(['openalex'])
    expect(service.get('openalex')?.label).toBe('OpenAlex')
    expect(service.get('cordis-projects')).toBeUndefined()
    await kernel.stop()
  })

  it('enables every adapter by default (schemastery fills the config tree)', async () => {
    const kernel = createKernel({ name: 'sources-defaults' })
    await kernel.start()
    await kernel.ctx.plugin(sources, {})

    const service = kernel.ctx.get('sources') as SourcesService
    expect(service.list().map((a) => a.id)).toEqual(['openalex', 'cordis-projects'])
    await kernel.stop()
  })

  it('clears the registry on dispose so stale service refs stop resolving adapters', async () => {
    const kernel = createKernel({ name: 'sources-dispose' })
    await kernel.start()
    await kernel.ctx.plugin(sources, {})
    const service = kernel.ctx.get('sources') as SourcesService
    expect(service.list().length).toBeGreaterThan(0)

    await kernel.stop()
    // effect disposer 清空注册表：旧引用不能再查到已下线的源
    expect(service.list()).toEqual([])
    expect(service.get('openalex')).toBeUndefined()
  })
})
