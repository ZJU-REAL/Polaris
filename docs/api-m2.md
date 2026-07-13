# M2 API 契约 — 文献调研 Research Wiki（前后端共同遵守）

延续 docs/api-m1.md 的总则。所有路由挂 `/api`，JWT Bearer。

## 1. Papers（论文库）

- `GET /projects/{pid}/papers?status=&q=&sort=relevance|-published_at&page=&size=20`
  → `{items: PaperRead[], total, page, size}`
- `GET /papers/{id}` → `PaperDetail`（含 wiki_content、concepts）
- `PATCH /papers/{id}` `{status?}`（人工纳入/排除：`included|excluded`）

`PaperRead`: `{id, project_id, title, authors: [{name}], year, venue, arxiv_id, doi, url,
published_at, relevance_score(0-1|null), status, tldr, has_wiki: bool, created_at}`
`status` 枚举：`candidate | scored | excluded | fetched | compiled | included`
`PaperDetail = PaperRead & {abstract, wiki_content(markdown，双链为 [[概念名]]), pdf_available: bool, concepts: [{id, name, category}]}`

## 2. Concepts（概念库）

- `GET /projects/{pid}/concepts?category=&q=` → `ConceptRead[]`
- `GET /concepts/{id}` → `ConceptRead & {wiki_content, papers: [{id, title, year}], related: [{id, name}]}`

`ConceptRead`: `{id, project_id, name, category: method|architecture|methodology|problem|metric|dataset|other, definition(一句话), paper_count}`

## 3. Search（检索）

- `GET /projects/{pid}/search?q=&mode=keyword|semantic&limit=20`
  → `{papers: [{...PaperRead, score}], concepts: [{...ConceptRead, score}]}`
  - `semantic` 模式用 pgvector（embedding stage 路由）；sqlite/无 embedding 时回退 keyword，响应带 `"mode_used"`

## 4. Ingest（冷启动 / 增量）——复用 Voyage 机制

- `POST /projects/{pid}/ingest` body：

```json
{
  "mode": "bootstrap" | "incremental",
  "knobs": {
    "months_back": 6,          // bootstrap 回填月数
    "max_papers": 50,          // 本次最多精读编译篇数（成本上限）
    "relevance_threshold": 0.6,
    "snowball_depth": 1,       // 引文雪球层数 0-2
    "compile_top_n": 20        // 打分后精读编译前 N 篇
  }
}
```

→ `VoyageRead`（kind=`wiki_bootstrap`|`wiki_ingest`，前端跳转 voyage 详情页看进度，SSE 复用 M1）

- `GET /projects/{pid}/ingest/state` → `{watermark: iso日期|null, last_run: {voyage_id, status, finished_at}|null, paper_counts: {candidate, scored, compiled, excluded, included, total}, running_voyage_id|null}`

约定：同一项目同时只允许一个 ingest voyage 在跑（409）。每日定时增量由 ARQ cron 触发（对 `definition.cadence=="daily"` 且已 bootstrap 过的项目）。

## 5. Obsidian 导出

- `GET /projects/{pid}/export/obsidian` → `application/zip` 下载
  vault 结构：`index.md`、`papers/<slug>.md`（frontmatter: title/arxiv_id/year/relevance/status/concepts）、`concepts/<slug>.md`、`trends.md`（占位）；正文含 `[[wikilink]]`

## 6. Dashboard 统计（顺带真实化）

- `GET /projects/{pid}/stats` → `{papers_total, papers_today, ideas_candidate, gates_pending, recent_activities: [{id, kind, message, created_at}]}`
  （Activity 表在 ingest/gate/voyage 关键节点写入）

## 7. 后端内部约定（前端可忽略）

- 文献源客户端：`services/literature/{arxiv,semantic_scholar,openalex}.py`，Redis 缓存（TTL 24h）+ 令牌桶限流（S2 免 key 100req/5min 的 80%）
- ingest pipeline 以 Voyage steps 呈现（Navigator 用固定计划模板，不靠 LLM 自由规划）：
  `检索候选 → 引文雪球 → 相关性打分(LLM, stage=relevance) → 水位线过滤+去重 → 下载PDF+抽全文(PyMuPDF) → Librarian 编译(LLM, stage=librarian, 全文优先, 中文, 产出 wiki markdown + [[概念]]) → 概念上链 + embedding → 更新水位线`
  每篇论文独立失败不中断批处理；checkpoint 记录已处理 id 列表，断点续跑
- Paper 新增列：source, external_ids JSON, published_at, tldr, pdf_path, full_text_path, embedding(pgvector, postgres-only variant), ingest 相关时间戳；Project 增加 ingest_state JSON
- Librarian 输出解析 `[[...]]` 双链 → upsert Concept + PaperConcept；概念页由 LLM 给一句话定义（首次出现时）
- embedding：LLM 层新增 `embed()`（openai_compat /embeddings + fake 确定性向量），stage=embedding
- migration：postgres 上 `CREATE EXTENSION IF NOT EXISTS vector`（按方言守卫）
