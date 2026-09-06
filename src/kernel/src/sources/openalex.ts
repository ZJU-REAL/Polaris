/* ============================================================
   OpenAlex 适配器：kernel 直连 api.openalex.org。

   为什么直连而不经 legacy 后端代理（#604 的 baseUrl）：后端的
   OpenAlex 能力只暴露在两处——admin 健康探针（limit=1、只回计数不回
   记录、要管理员身份）和库级异步 discovery run 管线（建 run→跑批→查
   hits，绑库绑用户）。两者都不是「同步搜一把拿记录」的形状，为代理
   而新开后端端点又违背 P2「能力上移」的方向，所以这里带上自己的
   HTTP 客户端。字段归一化口径抄后端 openalex.py 的 _simplify（摘要
   从 abstract_inverted_index 重建、DOI 去 https://doi.org/ 前缀、
   authorships→authors+affiliations），保证两边入库语义一致。

   免 key，走 polite pool：带 UA，配了 mailto 就一并带上（OpenAlex
   官方建议，能进更快的服务池）。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import type { FetchImpl, SourceAdapter, SourceAuthor, SourceRecord, SourceSearchOptions } from './contract.ts'

export const OPENALEX_BASE = 'https://api.openalex.org'

/** arXiv 论文的 DataCite DOI 前缀（对齐后端 ARXIV_DOI_TEMPLATE）。 */
const ARXIV_DOI_PREFIX = '10.48550/arxiv.'

const DEFAULT_LIMIT = 10

export interface OpenAlexAdapterOptions {
  /** 换基址仅供测试/私有镜像；默认官方 API。 */
  baseUrl?: string
  /** polite pool 联系邮箱；空则匿名池。 */
  mailto?: string
  fetchImpl?: FetchImpl
}

/** OpenAlex work 原始 JSON（只声明用到的字段）。 */
interface OpenAlexWork {
  id?: string
  title?: string | null
  publication_year?: number | null
  publication_date?: string | null
  doi?: string | null
  cited_by_count?: number
  abstract_inverted_index?: Record<string, number[]> | null
  primary_location?: {
    landing_page_url?: string | null
    source?: { display_name?: string | null } | null
  } | null
  authorships?: {
    author?: { display_name?: string | null } | null
    institutions?: { display_name?: string | null }[] | null
  }[]
}

/** 摘要重建：inverted index 是 {token: [位置…]}，按位置排序拼回原文。 */
function rebuildAbstract(inverted: Record<string, number[]> | null | undefined): string | undefined {
  if (!inverted || typeof inverted !== 'object') return undefined
  const tokens: [number, string][] = []
  for (const [token, positions] of Object.entries(inverted)) {
    if (!Array.isArray(positions)) continue
    for (const position of positions) {
      if (Number.isInteger(position)) tokens.push([position, token])
    }
  }
  if (!tokens.length) return undefined
  tokens.sort((a, b) => a[0] - b[0])
  return tokens.map(([, token]) => token).join(' ')
}

function normalizeAuthors(work: OpenAlexWork): SourceAuthor[] {
  const authors: SourceAuthor[] = []
  for (const authorship of work.authorships ?? []) {
    const name = authorship?.author?.display_name
    if (!name) continue
    // 机构去重保序，抄后端 dict.fromkeys 的语义
    const affiliations = [
      ...new Set(
        (authorship.institutions ?? [])
          .map((inst) => inst?.display_name)
          .filter((v): v is string => Boolean(v)),
      ),
    ]
    authors.push(affiliations.length ? { name, affiliations } : { name })
  }
  return authors
}

/** 导出仅为可测性：work JSON → SourceRecord 的纯映射。 */
export function openAlexRecord(work: OpenAlexWork): SourceRecord {
  // id 统一存短形（W…）：短形能直接拼回 /works/{id}，fetchByIds 才能闭环
  const id = (work.id ?? '').replace(/^https:\/\/openalex\.org\//, '')
  const doi = (work.doi ?? '').replace(/^https:\/\/doi\.org\//, '') || undefined
  // arXiv id 从 DataCite DOI 反推（10.48550/arXiv.<id>），后端同款约定
  const arxivId = doi?.toLowerCase().startsWith(ARXIV_DOI_PREFIX)
    ? doi.slice(ARXIV_DOI_PREFIX.length)
    : undefined
  const record: SourceRecord = {
    id,
    source: 'openalex',
    title: work.title ?? '',
    authors: normalizeAuthors(work),
    year: work.publication_year ?? null,
    venue: work.primary_location?.source?.display_name ?? null,
    extra: {
      citedByCount: work.cited_by_count ?? 0,
      publishedDate: work.publication_date ?? null,
      openalexId: work.id ?? null,
    },
  }
  if (doi) record.doi = doi
  if (arxivId) record.arxivId = arxivId
  const abstract = rebuildAbstract(work.abstract_inverted_index)
  if (abstract) record.abstract = abstract
  const url = work.primary_location?.landing_page_url ?? work.doi ?? undefined
  if (url) record.url = url
  return record
}

export class OpenAlexAdapter implements SourceAdapter {
  readonly id = 'openalex'
  readonly label = 'OpenAlex'
  readonly #baseUrl: string
  readonly #mailto?: string
  readonly #fetch: FetchImpl

  constructor(options: OpenAlexAdapterOptions = {}) {
    this.#baseUrl = (options.baseUrl ?? OPENALEX_BASE).replace(/\/+$/, '')
    this.#mailto = options.mailto || undefined
    this.#fetch = options.fetchImpl ?? globalThis.fetch
  }

  #headers(): Record<string, string> {
    // polite pool 要求可辨识的 UA；mailto 同时进 UA 与查询参数
    const contact = this.#mailto ? ` (mailto:${this.#mailto})` : ''
    return { 'user-agent': `polaris-kernel/0.1${contact}` }
  }

  #withMailto(url: URL): URL {
    if (this.#mailto) url.searchParams.set('mailto', this.#mailto)
    return url
  }

  async search(query: string, opts?: SourceSearchOptions): Promise<SourceRecord[]> {
    const limit = Math.max(1, opts?.limit ?? DEFAULT_LIMIT)
    const url = new URL(`${this.#baseUrl}/works`)
    url.searchParams.set('search', query)
    url.searchParams.set('per-page', String(limit))
    const res = await this.#fetch(this.#withMailto(url), { headers: this.#headers() })
    if (!res.ok) throw new Error(`openalex: search failed with HTTP ${res.status}`)
    const data = (await res.json()) as { results?: unknown[] }
    return (data.results ?? [])
      .filter((row): row is OpenAlexWork => Boolean(row) && typeof row === 'object')
      .map(openAlexRecord)
  }

  async fetchByIds(ids: string[]): Promise<SourceRecord[]> {
    // 逐个 GET /works/{id}：官方文档记载的形状，404 即「没有这条」跳过；
    // 其余错误如实抛出——静默吞掉服务端故障会把「源挂了」伪装成「查无此文」
    const records: SourceRecord[] = []
    for (const id of ids) {
      const url = new URL(`${this.#baseUrl}/works/${encodeURIComponent(id)}`)
      const res = await this.#fetch(this.#withMailto(url), { headers: this.#headers() })
      if (res.status === 404) continue
      if (!res.ok) throw new Error(`openalex: fetch ${id} failed with HTTP ${res.status}`)
      records.push(openAlexRecord((await res.json()) as OpenAlexWork))
    }
    return records
  }
}
