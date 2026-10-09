# Polaris Documentation

Polaris runs the entire research lifecycle in one desktop app: literature survey, idea
generation, idea review, experiment building on real GPU servers, LaTeX paper writing, and paper
review. It is built for individual researchers and runs entirely on your own computer, with a local
engine and no server to set up.
The heavy lifting (crawling, parsing, deduplication, metric parsing, citation matching) is
deterministic code; LLMs are reserved for the judgement calls (scoring, synthesis, drafting, review).
Every long task runs as a **Voyage**: a persisted, resumable, human-gated agent run that can span
hours or days without losing state.

For a high-level tour of the product and its feature set, start with the
[project README](../README.md). The documents below go deeper.

## Table of contents

| Document | What it covers |
| --- | --- |
| [Introduction](index.md) | What Polaris is, what makes it different, and a stage-by-stage overview of the pipeline with links to each guide. The docs-site homepage. |
| [Getting Started](getting-started.md) | Installing the desktop app, the first launch, giving Polaris a model (an agent backend or a model API), first steps in the app, and common first-run problems. |
| [Core Concepts](concepts.md) | The six-stage research pipeline, the Voyage long-running agent (Navigator / Helm / Sextant), the skill system, and the MCP read-only tool layer, explained in depth. |
| [Literature guide](literature.md) | The literature stage in practice: direction libraries and their inclusion config, ingest runs, the daily arXiv feed, wikis, and search. |
| [Ideas guide](ideas.md) | The idea and idea-review stages: Idea Forge gap analysis, scoring, the research proposal builder, and the Elo review tournament. |
| [Experiments guide](experiments.md) | The experiment stage: SSH credentials, intake, compute-budget gates, runs with streamed logs and metrics, and the iterate loop. |
| [Writing guide](writing.md) | The paper-writing stage: LaTeX projects and templates, collaborative editing, agent drafting bound to real metrics and citations, and compilation. |
| [Paper review guide](paper-review.md) | The paper-review stage: citation existence and support verification, number fact-checking, and the multi-perspective reviewer agents. |
| [PolarisBuddy](buddy.md) | The in-app assistant: chat, plan, and goal modes, the tool loop, page context, and per-user memory. |
| [Skills](skills.md) | Packaging agent behavior as data: skill types, injection points, the marketplace, and per-run snapshots. |
| [The Task System](task-system.md) | The long-running agent tasks (`VoyageRun`): the data model, the three run modes, library vs. topic tasks, who can see what, the action registry, acceptance checks, checkpoints, budgets, logs, failure handling, and the cron schedules. |
| [Literature Management](literature-management.md) | The single content pool and the four collections on top of it (direction library, topic shelf, personal library, daily feed), library ownership and management rights, tags, the trash, and the paper lifecycle: download, extract, chunk, embed, extract figures, compile, delete + orphan GC. |
| [Wikis & Concepts](wiki-and-concepts.md) | How a paper's wiki gets written (the three compile triggers, full text vs. abstract, figures and `![[fig:N]]`, why compiling carries no library context) and how concepts come out of it: names harvested from `[[wikilinks]]` in the prose, definitions batched to an LLM, placeholders and backfill, relink, orphan cleanup, plus the `paper_wikis` / `concepts` / `paper_concepts` model and concept scoping. |
| [Embedding & Retrieval](embedding-and-retrieval.md) | The two vector representations (paper-level vs full-text chunks), how and when each is built, the `chat_fulltext_index` opt-in, and how semantic search and literature chat retrieve (pgvector where available, graded fallback to keyword). |
| [MCP](mcp.md) | Connecting Claude Code, Codex, or Cursor to your research workspace: the two transports, topic scoping and permissions, the read-only tool catalog, recipes, limits, and how to check a connection. |
| [Agent backends](agents.md) | Using Claude Code, Codex, Gemini CLI and other ACP agents in the assistant and as Polaris's model: setup, permissions, and how they answer model calls. |
| [Architecture](architecture.md) | The public-facing system design: the single-process local engine, layered backend, in-process task queue and scheduler, LLM abstraction with DB model routing, the deterministic-vs-judgemental split, data stores, and real-time channels. |
| [Configuration](configuration.md) | How the engine gets its environment, the configuration layers, and the environment variables it reads (secrets, SQLite path, data directory, literature APIs, proxies), plus model routing. |
| [Development](development.md) | Working from source: repo layout, running the engine and frontend, migrations, tests, linting, the test engine image, the layering convention, and the branch-per-feature Git workflow. |
| [Desktop](desktop.md) | The Electron desktop app: process model, how it bootstraps the local engine, where the data lives, the `app://` protocol, the IPC contract, packaging, and unsigned-distribution notes. |

## Where things live

- Source code: `src/backend/` (the engine: FastAPI app package `app`, long-task functions in
  `worker`), `src/frontend/` (React + Vite), `src/desktop/` (the Electron shell), and `src/kernel/`
  (the plugin kernel).
- `docker/`: the dev/test engine image only (desktop smoke/E2E, golden recording); not a deployment.
- These docs: `docs/`.
