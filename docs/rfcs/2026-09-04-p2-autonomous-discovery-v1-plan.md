# P2 Implementation Plan: Autonomous Discovery v1 (Literature Domain)

| | |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-04 |
| Tracking | #635 |
| Design | [Polaris 2.0 design report](2026-09-02-polaris-2.0-design-report.zh.md) §8.2, §10–12, §23 |

P1 delivered the personal shell: kernel + desktop profile + packaged backend, de-labbed
and covered by the golden-transcript harness and a shell E2E. P2 delivers the product
core promised by the positioning — **given a research direction, the agent surveys
autonomously and produces a grounded research proposal** — in the literature domain,
on the existing Voyage engine (the process-pack interpreter stays in P3).

## Exit criteria

- A `discovery` run walks direction → hypothesis tree → grounded/novelty-checked/
  feasibility-checked proposal, end to end, with the fake provider (deterministic,
  golden-recordable) and with a real provider.
- Every hypothesis renders as support/refute/speculation evidence cards; every cited
  paper id resolves to a real library record (zero fabricated citations by
  construction: retrieval is deterministic, the LLM only selects).
- Zotero import brings an existing personal library in; library QA answers through
  the agentic RAG path with grounded citations.

## Work breakdown

### Track D — discovery loop core

- **D1 hypothesis tree entity.** `hypothesis_nodes` table (run-scoped tree:
  parent_id, kind hypothesis|experiment|analysis, statement, grounding JSON,
  novelty_report, feasibility, score, status open|expanded|pruned|validated|refuted,
  artifacts), migration + CRUD service + tree read API. No engine coupling yet.
- **D2 discovery kind.** New voyage kind `discovery`: Navigator plans over the tree
  (expand best open node / prune / stop) instead of a linear plan; recovery resumes
  from the best unexpanded node. Budget is recorded per node/rollout but not
  enforced (deferred by decision).
- **D3 four-stage hypothesis pipeline** (deterministic vs judgment split is the
  architectural invariant):
  1. *generate* — background + multi-path inspiration retrieval (semantic neighbors,
     graph-distant entities) → LLM composes; heavy sampling then dedup.
  2. *ground* — LLM splits a hypothesis into subclaims; each binds to retrieved real
     literature ids (retrieval is deterministic; unsupported subclaims are marked
     speculation).
  3. *novelty* — per-subclaim iterative search + judge comparison.
  4. *feasibility* — deterministic resource matching against the library; LLM only
     argues risk.
- **D4 minimal tournament** (optional depth mode): pairwise hypothesis comparison +
  critic; compute investment is the quality knob. Off by default.
- **D5 evidence-card surface.** Frontend: tree view for a discovery run; per-node
  evidence cards (support/refute/speculation) with citation + snippet links;
  support degree = share of subclaims covered by independent literature.
- **D6 disclosure generator.** A run report artifact: what was searched, what was
  read, which branches were pruned and why — pruned/failed branches are retained by
  construction (anti-cherry-picking).

### Track E — literature engine layers ① ③ ⑤

- **E1 parsing dual-track (①).** A parsing-adapter seam in the Python edge:
  GROBID (metadata/citations) and a MinerU-class body parser as optional adapters
  with the existing pdf_extract as the always-available fallback; per-document
  quality report decides which output wins. Adapters run as configurable services
  (docker on dev/server; desktop degrades to fallback until packaged).
- **E2 association layer (③).** Citation-intent classification (cheap, run on the
  whole library), OpenAlex bibliographic alignment for imported records, on top of
  the existing concept-linking and evidence-anchor groundwork.
- **E3 agentic RAG (⑤).** Four pieces on the library index: iterative query
  expansion, LLM rerank + contextual summarization, citation-graph traversal for
  recall, evidence-first answering with forced citation grounding. Serves library
  QA directly and stages 2–3 of D3. Small stable libraries (<200k tokens) bypass to
  full-context.
- **E4 source adapters as kernel plugins.** Move the source-adapter registry seam
  to TS kernel plugins calling the Python edge; ship OpenAlex (main trunk) and the
  EU CORDIS project source as the first two; existing arXiv/etc. adapters keep
  working behind the seam.
- **E5 Zotero import.** BetterBibTeX/Zotero export (RDF/BibTeX + attachments) into
  a library, dedup against existing records, then the normal enrich pipeline.

## Sequencing

Wave 1 (parallel, independent): D1 · E5 · E2.
Wave 2: E3 (needs the index; feeds everything) · D2 (needs D1).
Wave 3: D3 (needs D2+E3) · E4 · E1.
Wave 4: D4 · D5 (needs D3 output shapes) · D6.

Each PR: one issue, one branch from `origin/main`, full backend suite against the
#581 baseline, goldens byte-identical unless the PR intentionally extends the
recorded chains (a discovery-run golden is added in wave 3), frontend build green.

## Non-goals (deferred)

Extraction layer ② and the four precomputed fuels (P2.5); process-pack interpreter,
Runner v2, experiments (P3); contract freeze and marketplace (P4); budgets, approval
gates, grant/patent records (deferred by decision).
