# RFC amendment: literature fusion implementation status and follow-up series

| Field | Value |
| --- | --- |
| Status | Proposed |
| Parent RFC | #448 |
| Tracking issue | #471 |
| Target repository | ZJU-REAL/Polaris |
| Updated | 2026-08-26 |

## Purpose

The parent RFC defines the architecture for library-scoped discovery, PDF
assets, full-text evidence, Polaris Extension, and interdisciplinary research.
This amendment records what the first implementation series contains and what
is still required before the integrated literature workflow is a usable
product. It does not change the parent RFC's data boundaries, permission model,
or migration rules.

The implementation series is intentionally split by responsibility. A merged
contract or persistence PR does not claim that a provider, worker, frontend, or
reader feature is already production-ready.

## Coverage of the first series

| PR | Boundary | Current coverage |
| --- | --- | --- |
| #450 | Discovery contracts | Search runs, source attempts, candidate persistence, snapshots, and candidate identity contract |
| #452 | Query ranking | Deterministic query planning, normalization, deduplication, score dimensions, and result tiers |
| #463 | Discovery API | Library-scoped run APIs, candidate filtering, progress reads, and write/read permissions |
| #464 | PDF assets | Content-addressed blobs, paper assets, grants, identity checks, and scoped reuse |
| #465 | Content lifecycle | Immutable parse versions, Markdown/text/chunks, MinerU adapter boundary, PyMuPDF fallback state, and vectors |
| #466 | Evidence anchors | Version-aware sentence/paragraph/chunk anchors and paper-level fallback metadata |
| #467 | Download protocol | User API keys, multi-paper batches, lease/retry/idempotent archive, and independent library-paper bindings |
| #468 | Polaris Extension | Browser-side batch bridge, local PDF cache, checksum/identity validation, and batch archive client |
| #469 | Interdisciplinary profile | Versioned scope draft/confirmation, project research mode, and dedicated cross-disciplinary library |
| #470 | Interdisciplinary Skill | Builtin skill-market workflow for scope, retrieval, evidence, ideas, experiments, writing, and review |

These PRs provide backend contracts and one extension client. They do not yet
provide the Polaris main-site discovery screen, real multi-source runtime
orchestration, MinerU Cloud production client, or universal evidence injection
into every AI workflow.

## Required follow-up series

Each row becomes one Issue, one branch, and one PR. New branches must start
from the current `origin/main`, and stacked branches are permitted only when a
reviewer explicitly requests them. After a dependency lands, the child branch
must be rebased and parent files removed from its final diff.

### Runtime and provider series

1. **Discovery runtime adapters**
   - Execute persisted query plans through OpenAlex, Semantic Scholar, arXiv,
     and PubMed.
   - Preserve requested count separately from internal candidate budget.
   - Pass start/end years to every capable source.
   - Record source progress, retries, rate limits, authentication failures, and
     partial results.
   - Reuse existing Polaris clients and YFR-derived provider logic through a
     narrow adapter interface. Do not copy the standalone YFR application.

2. **Extended sources and credential health**
   - Add Crossref, Europe PMC, HAL, CORE, BASE, Unpaywall, and Sciverse
     adapters where credentials and service contracts permit.
   - Support encrypted key pools, rotation, per-source connection tests, and
     source-level capability reporting.
   - Keep missing metadata null; never use `Unknown journal` as a data value.

3. **OA discovery cache and promotion**
   - Download every verifiable OA candidate into a temporary, content-addressed
     cache during discovery.
   - Do not parse or vectorize an unpromoted candidate.
   - On user promotion, validate identity and convert the cache into a formal
     `PaperAsset` and processing job idempotently.

### Polaris product series

4. **Library discovery workspace**
   - Add the discovery entry to the existing library navigation.
   - Show title, abstract, authors, venue, DOI, OA state, source evidence,
     score dimensions, inclusion rationale, progress, history, filters, and
     sorting.
   - Enforce creator/curator write access and public-library read-only access.

5. **Administrator literature settings**
   - Add source enablement, query limits, year defaults, scoring rubric,
     provider keys, key-pool editing, and connection-test dialogs.
   - Reuse the existing LLM settings interaction style instead of introducing a
     second settings framework.

### Processing and evidence series

6. **MinerU Cloud production adapter**
   - Implement upload, acceptance, processing, result download, polling,
     multi-key rotation, concurrency limits, retry windows, and persisted
     status transitions.
   - Start the timeout after MinerU accepts the task. Fall back to PyMuPDF only
     after a permanent error or exhausted retries.
   - Reparse by creating a new content version; retain the previous active
     version until replacement succeeds.

7. **Reader and PDF evidence navigation**
   - Render persisted Markdown, figures, and tables safely.
   - Resolve sentence anchors to structured content and PDF text-layer
     coordinates, with quote matching and paper-page fallback.
   - Add visible parsed/vector status and retry controls.

8. **Workflow evidence injection**
   - Make Wiki, chat, ideas, experiments, writing, review, presentations, and
     MCP use the same authorized content-version context and evidence manifest.
   - Do not emit a fine-grained citation when the source text cannot resolve it.
     Use an explicit paper-level fallback instead.

### Interdisciplinary runtime series

9. **Profile orchestration**
   - Repair invalid LLM scope responses into editable drafts.
   - Persist query matrices, primary/associated subject boundaries, bridge-paper
     coverage, and profile-version references.
   - Run per-subject retrieval before cross-subject fusion and report coverage
     separately from relevance.

10. **Skill injection and regression**
    - Bind a project to a profile version and Skill template version.
    - Carry the profile constraints into idea, experiment, writing, review, and
      presentation workflows.
    - Keep conventional project behavior unchanged.

## Merge and review gates

The recommended dependency order is:

```text
#450 -> #452 -> #463
#464 -> #465 -> #466
             └-> #467 -> #468
#469 -> #470
runtime adapters -> extended sources -> OA promotion
discovery UI -> admin settings
MinerU production -> reader evidence -> workflow injection
profile orchestration -> Skill injection
```

Every implementation PR must include:

- an Issue link using `Closes #...` or `Fixes #...`;
- a single behavior boundary and an explicit non-goal list;
- focused tests plus compatibility tests for existing paper/library flows;
- migration upgrade/downgrade coverage when schema changes;
- Ruff, compile, frontend/build checks where applicable;
- a sensitive-file scan with no credentials, PDFs, ZIPs, databases, caches, or
  deployment artifacts;
- a note explaining whether the branch is independent or temporarily stacked.

The candidate gate remains strict: unpromoted search hits must not create
formal papers, assets, parse jobs, vectors, or Wiki content. Private and
institution-authorized PDFs remain grant-scoped even when DOI and bytes match
an asset in another library.

## Compatibility statement

This amendment does not require the author to merge the entire integrated
snapshot. Each PR can be reviewed and merged independently in the order above.
The frozen YFR-integrated directory remains a behavior reference only. Runtime
code is reimplemented against current Polaris interfaces, with no external YFR
deployment dependency and no copied credentials.
