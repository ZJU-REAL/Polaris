/* ============================================================
   文献源适配器契约（#646，P2 E4）。

   把「从某个外部源检索文献/项目并归一化」抽成 kernel 级契约，后续
   各源（OpenAlex / CORDIS projects / arXiv / S2 …）都以插件适配器的
   形式挂进来，上层（desktop、市场装载的配置树）只面向这一个形状。

   SourceRecord 的字段口径对齐后端 app/schemas/paper.py 的 PaperRead
   （title/authors/year/venue/arxiv_id/doi/url…），只是命名转成 TS 惯用
   的 camelCase：两边语义一致，未来 kernel 记录喂回后端入库时不需要
   再做一层字段翻译。year/venue 学后端用「必有键、可为 null」而不是
   可选键——消费方可以无脑读，不用先探测键存在性。
   ============================================================ */

/** 作者：对齐后端 AuthorRead（name + 尽力而为的机构列表）。 */
export interface SourceAuthor {
  name: string
  /** 该作者的所属机构；源没给就省略。 */
  affiliations?: string[]
}

/** 各源检索结果的归一化记录。 */
export interface SourceRecord {
  /** 源内原生 id（OpenAlex 短 id `W…`、CORDIS 项目号）。必须能原样喂回 fetchByIds。 */
  id: string
  /** 产出该记录的适配器 id，冗余进记录便于混排后溯源。 */
  source: string
  title: string
  authors: SourceAuthor[]
  year: number | null
  venue: string | null
  doi?: string
  arxivId?: string
  abstract?: string
  url?: string
  /** 各源独有、不值得归一化的字段（被引数 / funding / programme 等）。 */
  extra?: Record<string, unknown>
}

export interface SourceSearchOptions {
  /** 最多返回多少条；各适配器自己映射到源的分页参数。 */
  limit?: number
}

/** 单个文献源适配器。实现必须无共享可变状态：注册表可随插件重载重建。 */
export interface SourceAdapter {
  /** 稳定 id（配置键 / SourceRecord.source 都用它），kebab-case。 */
  id: string
  /** 给人看的名字（设置界面用）。 */
  label: string
  /** 关键词检索。 */
  search(query: string, opts?: SourceSearchOptions): Promise<SourceRecord[]>
  /** 按源内 id 批量取全量记录；不存在的 id 静默跳过（部分成功优于整批失败）。 */
  fetchByIds(ids: string[]): Promise<SourceRecord[]>
}

/** 适配器出网的 fetch 形状。构造时可注入，单测用 fixture 替身不打真网。 */
export type FetchImpl = typeof globalThis.fetch
