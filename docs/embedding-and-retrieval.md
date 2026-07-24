# Embedding & Retrieval

This document describes how Polaris turns papers into vectors and how those vectors power search and
literature chat. It builds on [Literature Management](literature-management.md), which covers the
content pool, the four collections, and the lifecycle steps that produce these vectors.

## Two kinds of embedding

Polaris keeps **two distinct vector representations** per paper, at different granularities. They are
never mixed: a query is compared only against vectors of the same kind.

| | Paper-level embedding | Chunk embedding |
| --- | --- | --- |
| Column | `papers.embedding` (one vector per paper) | `paper_chunks.embedding` (one per ~1200-char chunk, ≤120/paper) |
| Text embedded | title + authors + abstract (see below) | the chunk's full-text slice (truncated to 2000 chars) |
| Model / dim | BGE-M3, 1024-dim | BGE-M3, 1024-dim |
| Purpose | Paper-level semantic search; similarity / dedup | Fine-grained retrieval for literature chat (find the relevant passage) |
| Cost | Cheap (one vector, batched) | Heavy (dozens of vectors per paper; needs the full text) |

### The paper-level embedding text

The paper-level vector is computed from a short, deterministic text. **The formula must be identical
everywhere a paper-level vector is produced**, otherwise queries and documents land in inconsistent
vector spaces and cosine ranking degrades.

The formula lives in one shared helper, `paper_enrich.py::paper_embedding_text(paper)` =
**title + author names + abstract**, truncated to 2000 chars. All three producers call it:
`embed_paper` (manual add / Daily collect / fetch-PDF), the ingest `wiki.link_concepts` batch, and
the daily-feed batch.

Author names are part of the text so that "find work by X" style queries land. The previous formula
was `title + tldr + abstract`, which was inconsistent in practice because `tldr` only exists on
compiled papers. Existing vectors are deliberately **not** re-embedded: the difference is dominated
by title + abstract, so search stays usable while old vectors converge naturally as papers are
recompiled or re-indexed.

The **query** side embeds the user's search text as-is (`get_llm_router().embed([q])`); the
query-vs-document asymmetry is normal — only the document formula needs to be consistent.

## When each vector is built

See the path × step table in [Literature Management](literature-management.md#path--step-quick-reference).
In summary:

- **Paper-level embedding** is produced by every content-producing path: direction-library ingest
  (`link_concepts`, for papers still missing a vector), manual add / Daily collect (`enrich_paper`
  embed stage), and fetch-PDF. It is skipped when the paper already has an `embedding` (idempotent).
- **Chunk embedding** is heavier and is **gated by the per-user `chat_fulltext_index` opt-in**
  (`_require_fulltext_index_enabled`, `User.setting("chat_fulltext_index")`):
  - Direction-library ingest always embeds chunks for the library corpus.
  - The add paths (`enrich_paper`, `fetch_pdf`) create chunk rows but only embed them when the user
    has the opt-in on; otherwise the chunk rows sit with `NULL` embeddings, to be filled later.
  - Manual rebuild endpoints (`/projects/{id}/shelf/index/rebuild`, `/library/index/rebuild`) require
    the opt-in; `/projects/{id}/index/rebuild` (a direction library) is a synchronous maintenance
    endpoint that returns `{indexed, embedded, skipped}`.
- **Daily-feed papers are embedded only when an admin turns it on.** `sync_daily_feed` builds
  lightweight rows with no LLM, so daily papers have no vector by default. The admin setting
  **`daily_feed_embed_enabled` (off by default)** makes each sync embed the papers it touched that
  still have a `NULL` vector, using the shared `paper_embedding_text` formula;
  `POST /admin/settings/daily-embed/backfill` fills the current window in one shot. Both are
  idempotent (an existing vector is never overwritten) and best-effort (a failed batch is logged and
  never breaks the sync). Chunk vectors are not built for daily papers — they have no full text.

## Retrieval

Postgres with pgvector is required for vector search: `semantic_search_supported(session)` and
`chunk_vector_search_supported(session)` return true only on `postgresql`. On SQLite (tests / no
pgvector) every path **degrades gracefully to keyword or summary retrieval** and never raises.

### Paper-level semantic search

Used by the library search box (keyword ↔ semantic toggle) and, by reuse, the related-work page.

1. Embed the query (`get_llm_router().embed([q])`).
2. `papers.py::semantic_search_papers` runs a pgvector cosine distance `1 - (p.embedding <=> qv)`,
   **JOINed to `library_papers`** and filtered to `p.embedding IS NOT NULL`. It is therefore
   **library-scoped** and only ranks papers that have both a membership and a vector.
3. `rerank_paper_rows` applies a lightweight rerank over the candidates.
4. `GET /libraries/{id}/search?mode=semantic` returns `SearchResponse{papers, concepts, mode_used,
   reranked}`; `mode_used` reports `keyword` when semantic was unavailable so the UI can show a
   fallback notice.

Because this query is library-membership-scoped, collections without library membership need their
own pgvector query over the right candidate set. Two exist:

- **Personal library** — `GET /me/library?mode=semantic` ranks the caller's **saved** entries whose
  `last_paper_id` points at a pool paper with a vector (`user_library.semantic_saved_entries`), then
  reranks. Coverage is inherently partial (an entry whose source paper is gone, or which was never
  embedded, cannot be ranked), and the UI says so.
- **Daily feed** — `GET /daily/papers?mode=semantic` runs a pgvector query over
  `daily_feed_entries ⨝ papers` with **no library join** (`daily_feed.semantic_search_daily`),
  honoring the date / category / announce filters, then reranks. It returns `mode_used` plus
  `vector_ready` / `vector_total` so the UI can state honestly how much of the pool is embedded.

Every semantic path falls back to keyword when pgvector is unavailable or the provider cannot embed,
reporting `mode_used="keyword"` rather than failing.

**Citation export** follows the same per-collection scoping: `papers_for_export` (project),
`papers_for_library_export`, `papers_for_personal_export` (caller's saved papers), and
`papers_for_daily_export` (current window) all feed the shared `build_bibtex` / `build_csl_json`
generators, each honoring an optional `ids` subset for multi-select export.

### Chunk retrieval for literature chat

The literature-chat surfaces (direction-library chat, course related-work chat, personal-library
chat, daily-feed chat) all build their context through `library_chat.py`, which retrieves passages
with a **graded fallback** (`_retrieve_chunks`) so any failure degrades instead of erroring:

1. **Chunk vector search** (`semantic_search_chunks`, pgvector, scoped to the given `paper_ids` or
   library) — the primary path when chunks + pgvector are available.
2. **Chunk keyword search** (`keyword_search_chunks`, `ILIKE` over chunk text) — when vectors are
   unavailable or fail.
3. **Summary fallback** — when no chunks are retrieved at all, feed the papers' `tldr` / abstract
   (bounded to `FALLBACK_PAPERS`) as context.

`build_scoped_messages` (used for an explicit `paper_ids` set — shelf, personal library, daily feed)
does the same, but its summary fallback selects the first `FALLBACK_PAPERS` papers **in the caller's
order** and feeds their `tldr` / abstract.

**Consequence for the daily-feed chat:** daily papers never have chunks (no full text), so both chunk
paths return nothing and the chat always lands on the summary fallback — it feeds the abstracts of
the first N daily papers **in list order, not by question relevance**. Enabling daily embeddings
makes the *search* semantic, but the chat's fallback still takes a blind prefix: ranking that
candidate set by the paper-level vectors is a deliberate follow-up, because `build_scoped_messages`
is shared by the shelf, personal-library and daily chats and changing it must not regress the others.

## Design principles

- **Two granularities, kept separate.** Paper-level for "which paper", chunk-level for "which
  passage". Never compare across kinds.
- **One document formula.** All paper-level vectors use the same `paper_embedding_text`; otherwise the
  space is inconsistent. Existing vectors are left to converge rather than force a costly re-embed.
- **Idempotent and skip-aware.** Never re-download, re-slice, or re-embed something that already
  exists (`pdf_path` / existing chunks / `embedding IS NOT NULL`). Collecting an already-embedded
  daily paper into a library skips the embed step and only does the missing work.
- **Opt-in for the expensive part.** Chunk embeddings (and daily embeddings) are the token-heavy
  pieces, so they are gated behind explicit settings rather than run for every paper.
- **Postgres for vectors, graceful degradation elsewhere.** Vector search needs pgvector; without it,
  search and chat fall back to keyword / summary retrieval and stay functional.
- **Honest coverage.** Index-build endpoints report `indexed` / `skipped (no full text)` so users can
  see that a fast build skipped most papers rather than silently indexing "everything".
