# RFC: Polaris 2.0 — A Deep Research Platform for Autonomous Scientific Discovery

| Field | Value |
| --- | --- |
| Status | Draft |
| Target repository | ZJU-REAL/Polaris |
| Tracking issue | #564 |
| Full design report | `docs/rfcs/2026-09-02-polaris-2.0-design-report.zh.md` (Chinese, authoritative) |
| Updated | 2026-09-02 |

## Summary

Polaris is rebuilt from a lab-oriented AI research application into a **Deep
Research platform for autonomous scientific discovery**. "Deep Research" here
means deep scientific research, not retrieval-style report writing: given a
research direction, the agent autonomously runs the full loop —
survey → hypothesis → experiment → analysis → iteration — and produces
validated findings. Any discipline can plug in its own data sources,
simulation tools, instruments, and research processes.

Design tenets: **everything is a plugin, everything composes, everything is
hot-swappable, everything evolves.** The first two and a half tenets are the
two composability axioms formalized by cordis (spatial: declarative dependency
assembly; temporal: fully revertible effects); the fourth is this platform's
addition — the agent's capability surface grows continuously through the
ecosystem, user-defined components, and agent-authored tools, not through
platform releases.

## Architecture

Three-layer kernel on a new runtime split:

1. **L1 plugin kernel** (Node/TS, vendored `@deepseek-ai/cordis`, MIT):
   plugin tree, service DI with inject gating, effect-reversible lifecycle,
   declarative config tree with cross-process reconciliation, schema-driven
   config UI, marketplace, MCP client hub. Runs in the Electron main process.
2. **L2 agent runtime** (the moat): the existing Voyage engine — durable
   plan-execute-verify state machine with checkpoint resume — promoted to a
   generic interpreter of declarative research-process packs; a new
   `discovery` kind operates on a first-class hypothesis/experiment tree.
3. **L3 research state layer** (the ecosystem's glue): typed records
   (paper/project/dataset), libraries, concepts, extractions, evidence
   anchors, provenance. Plugins interoperate by reading/writing this state,
   not by talking to each other.

Python remains as the **science edge** — PDF parsing, extraction,
instrument/simulation drivers, and the existing FastAPI backend mounted as a
legacy engine — supervised child processes speaking line-delimited JSON-RPC
(engine semantics) or MCP (tool semantics). Local-first data: SQLite (FTS5 +
sqlite-vec) plus a file-over-app layout where PDFs, notes, and exports live in
user-visible folders and the database holds only rebuildable indexes.

Seven extension points share one manifest: data sources, record kinds,
experiment runners, agent tools (MCP), process packs, discipline packs, and
UI panels. The literature engine is specified in five layers
(parse → extract → link → synthesize → apply); extraction schemas are
discipline-pack assets (PICO, task/dataset/metric, synthesis recipes,
reactions), and the engine precomputes structured fuel for hypothesis
generation (unlinked concept pairs, contradiction/gap lists,
purpose–mechanism method index, negative results) with deterministic
literature grounding and novelty checks.

Deferred by explicit decision (mechanisms kept, not productized): approval
gates, cost budgeting, grant/patent record kinds.

## Migration

Strangler-style, product usable at every phase:

- **P0 spike** (hard fallback gate): cordis kernel drives one full legacy
  chain; SQLite+sqlite-vec benchmark at 100k/500k chunks; JSON-RPC seam +
  cross-process reconciler prototype. Any failure → fall back to a
  Python-kernel variant, cost limited to the spike.
- **P1**: personal Electron shell + kernel + single-user profile
  (`InlineTaskQueue` replaces Redis/ARQ; SQLite is already the default and
  the whole test suite runs on it); 15 independent de-lab removal PRs;
  record-replay regression harness seeded from the deterministic fake LLM
  provider.
- **P2/P2.5**: autonomous discovery v1 on the literature domain + literature
  engine build-out. **P3**: autonomous experiments (Runner v2, process-pack
  interpreter, BYO runners, OpenFOAM/ngspice → FMU → PyAnsys). **P4**:
  contract freeze (semver) then ecosystem opening. **P5**: monetization.

## Non-goals (this series)

Visual workflow canvas; subscription-with-included-quota pricing; commercial
EDA integration; multi-device sync (phase 2 of desktop); full-length survey
generation.

The Chinese design report is the authoritative specification; this RFC is its
summary of record for the repository.
