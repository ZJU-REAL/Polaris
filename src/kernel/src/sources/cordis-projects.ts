/* ============================================================
   EU CORDIS 项目适配器：直连 cordis.europa.eu 开放数据 API。

   注意别跟 vendor/deepseek-cordis（插件运行时框架）搞混：这里的
   CORDIS 是欧盟科研项目库（设计报告 §14「papers + EU projects」），
   后端目前完全没有对应适配器，所以只能 kernel 直连。

   API 形状均已对真实服务验证过（2026-09，见 #646）：
   - 检索：GET /api/search/results?q=<查询语言>&format=json&num=N&page=P
     查询语言形如 contenttype='project' AND 'graphene'；
     响应 {status, payload:{total, results:[{id, reference, acronym,
     title, teaser, programme:[{code,…}], startDate, endDate,
     coordinatedIn, rcn}]}}。列表里 startDate 是本地化模板串
     （"1 {{month_05}} 2018"），只能正则抠年份；不含协调机构名。
   - 详情：GET /project/id/{id}?format=json
     含 objective（全文目标）、identifiers.grantDoi（10.3030/<id>）、
     ISO 日期，协调机构在 relations.associations 里
     （contenttype='organization' 且 attributes.type='coordinator'）。

   归一化到论文口径的映射约定：objective/teaser→abstract、协调机构→
   authors（项目没有自然人作者，机构是最接近的署名主体）、框架计划
   （H2020/HORIZON）→venue；经费与 programme 明细进 extra。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import type { FetchImpl, SourceAdapter, SourceRecord, SourceSearchOptions } from './contract.ts'

export const CORDIS_BASE = 'https://cordis.europa.eu'

const DEFAULT_LIMIT = 10

export interface CordisProjectsAdapterOptions {
  /** 换基址仅供测试；默认官方站点。 */
  baseUrl?: string
  fetchImpl?: FetchImpl
}

/** 检索列表里的一条 project（只声明用到的字段）。 */
interface CordisSearchHit {
  id?: string
  reference?: string
  acronym?: string
  title?: string
  teaser?: string
  startDate?: string
  endDate?: string
  coordinatedIn?: string
  rcn?: string
  programme?: { code?: string; id?: string; title?: string }[]
}

/** 详情端点的 project JSON（只声明用到的字段）。 */
interface CordisProjectDetail {
  id?: string
  rcn?: string
  acronym?: string
  title?: string
  teaser?: string
  objective?: string
  keywords?: string
  status?: string
  totalCost?: string
  ecMaxContribution?: string
  startDate?: string
  endDate?: string
  identifiers?: { grantDoi?: string }
  relations?: { associations?: CordisAssociation[] }
}

interface CordisAssociation {
  contenttype?: string
  legalName?: string
  code?: string
  frameworkProgramme?: string
  title?: string
  attributes?: { type?: string }
}

/** 组装 CORDIS 查询语言。单引号是语法定界符，用户词里的一律替换成空格。 */
export function buildCordisQuery(query: string): string {
  const cleaned = query.replace(/'/g, ' ').replace(/\s+/g, ' ').trim()
  return `contenttype='project' AND '${cleaned}'`
}

/** 从各种日期形态里抠年份：列表是 "1 {{month_05}} 2018"，详情是 ISO。 */
function yearOf(value: string | undefined): number | null {
  const match = /(?:19|20)\d{2}/.exec(value ?? '')
  return match ? Number(match[0]) : null
}

function projectUrl(baseUrl: string, id: string): string {
  return `${baseUrl}/project/id/${id}`
}

/** 导出仅为可测性：检索列表条目 → SourceRecord 的纯映射。 */
export function cordisSearchRecord(hit: CordisSearchHit, baseUrl = CORDIS_BASE): SourceRecord {
  const id = String(hit.id ?? hit.reference ?? '')
  const programme = (hit.programme ?? []).map((p) => ({
    code: p.code ?? null,
    id: p.id ?? null,
    title: p.title ?? null,
  }))
  const record: SourceRecord = {
    id,
    source: 'cordis-projects',
    title: hit.title ?? '',
    // 列表响应没有协调机构名，authors 留空；要机构走 fetchByIds
    authors: [],
    year: yearOf(hit.startDate),
    venue: hit.programme?.[0]?.code ?? null,
    url: projectUrl(baseUrl, id),
    extra: {
      acronym: hit.acronym ?? null,
      rcn: hit.rcn ?? null,
      coordinatedIn: hit.coordinatedIn ?? null,
      programme,
      startDate: hit.startDate ?? null,
      endDate: hit.endDate ?? null,
    },
  }
  if (hit.teaser) record.abstract = hit.teaser
  return record
}

/** 导出仅为可测性：详情 JSON → SourceRecord 的纯映射。 */
export function cordisDetailRecord(detail: CordisProjectDetail, baseUrl = CORDIS_BASE): SourceRecord {
  const id = String(detail.id ?? '')
  const associations = detail.relations?.associations ?? []
  const coordinators = associations.filter(
    (a) => a.contenttype === 'organization' && a.attributes?.type === 'coordinator' && a.legalName,
  )
  const legalBasis = associations.filter(
    (a) => a.contenttype === 'programme' && a.attributes?.type === 'relatedLegalBasis',
  )
  const record: SourceRecord = {
    id,
    source: 'cordis-projects',
    title: detail.title ?? '',
    authors: coordinators.map((a) => ({ name: a.legalName! })),
    year: yearOf(detail.startDate),
    // 框架计划名（H2020/HORIZON）当 venue：它对项目的意义最接近「发表处」
    venue: legalBasis[0]?.frameworkProgramme ?? null,
    url: projectUrl(baseUrl, id),
    extra: {
      acronym: detail.acronym ?? null,
      rcn: detail.rcn ?? null,
      keywords: detail.keywords ?? null,
      status: detail.status ?? null,
      funding: {
        totalCost: detail.totalCost ?? null,
        ecMaxContribution: detail.ecMaxContribution ?? null,
      },
      programme: legalBasis.map((a) => ({ code: a.code ?? null, title: a.title ?? null })),
      startDate: detail.startDate ?? null,
      endDate: detail.endDate ?? null,
    },
  }
  const abstract = detail.objective || detail.teaser
  if (abstract) record.abstract = abstract
  if (detail.identifiers?.grantDoi) record.doi = detail.identifiers.grantDoi
  return record
}

export class CordisProjectsAdapter implements SourceAdapter {
  readonly id = 'cordis-projects'
  readonly label = 'EU CORDIS Projects'
  readonly #baseUrl: string
  readonly #fetch: FetchImpl

  constructor(options: CordisProjectsAdapterOptions = {}) {
    this.#baseUrl = (options.baseUrl ?? CORDIS_BASE).replace(/\/+$/, '')
    this.#fetch = options.fetchImpl ?? globalThis.fetch
  }

  async search(query: string, opts?: SourceSearchOptions): Promise<SourceRecord[]> {
    const limit = Math.max(1, opts?.limit ?? DEFAULT_LIMIT)
    const url = new URL(`${this.#baseUrl}/api/search/results`)
    url.searchParams.set('q', buildCordisQuery(query))
    url.searchParams.set('format', 'json')
    url.searchParams.set('num', String(limit))
    url.searchParams.set('page', '1')
    const res = await this.#fetch(url, { headers: { 'user-agent': 'polaris-kernel/0.1' } })
    if (!res.ok) throw new Error(`cordis-projects: search failed with HTTP ${res.status}`)
    const data = (await res.json()) as { payload?: { results?: unknown[] } }
    return (data.payload?.results ?? [])
      .filter((row): row is CordisSearchHit => Boolean(row) && typeof row === 'object')
      .map((row) => cordisSearchRecord(row, this.#baseUrl))
  }

  async fetchByIds(ids: string[]): Promise<SourceRecord[]> {
    // 详情端点才有 objective / 协调机构 / grantDoi，所以按 id 走详情而不是
    // 拿 id 再搜一遍；404 即「没有这个项目」跳过，其余错误如实抛出
    const records: SourceRecord[] = []
    for (const id of ids) {
      const url = new URL(`${this.#baseUrl}/project/id/${encodeURIComponent(id)}`)
      url.searchParams.set('format', 'json')
      const res = await this.#fetch(url, { headers: { 'user-agent': 'polaris-kernel/0.1' } })
      if (res.status === 404) continue
      if (!res.ok) throw new Error(`cordis-projects: fetch ${id} failed with HTTP ${res.status}`)
      records.push(cordisDetailRecord((await res.json()) as CordisProjectDetail, this.#baseUrl))
    }
    return records
  }
}
