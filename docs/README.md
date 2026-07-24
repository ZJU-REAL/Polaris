# Polaris Documentation

Polaris runs the entire research lifecycle as a single web application: literature survey, idea
generation, idea review, experiment building on real GPU servers, LaTeX paper writing, and paper
review. It is built for a research lab, with multi-user access, RBAC, and invite-code registration.
The heavy lifting (crawling, parsing, deduplication, metric parsing, citation matching) is
deterministic code; LLMs are reserved for the judgement calls (scoring, synthesis, drafting, review).
Every long task runs as a **Voyage**: a persisted, resumable, human-gated agent run that can span
hours or days without losing state.

For a high-level tour of the product and its feature set, start with the
[project README](../README.md). The documents below go deeper.

## Table of contents

| Document | What it covers |
| --- | --- |
| [Getting Started](getting-started.md) | Prerequisites, cloning, configuring `.env`, and running the full stack with `make dev` (Docker) or the no-Docker local path. |
| [Core Concepts](concepts.md) | The six-stage research pipeline, the Voyage long-running agent (Navigator / Helm / Sextant), the skill system, and the MCP read-only tool layer, explained in depth. |
| [Literature Management](literature-management.md) | The single content pool and the four collections on top of it (direction library, course shelf, personal library, daily feed), and the paper lifecycle: download, extract, chunk, embed, extract figures, compile, delete + orphan GC. |
| [Embedding & Retrieval](embedding-and-retrieval.md) | The two vector representations (paper-level vs full-text chunks), how and when each is built, the `chat_fulltext_index` opt-in, and how semantic search and literature chat retrieve (pgvector, graded fallback). |
| [Architecture](architecture.md) | The public-facing system design: layered backend, ARQ worker, LLM abstraction with DB model routing, the deterministic-vs-judgemental split, data stores, and real-time channels. |
| [Configuration](configuration.md) | Reference table of every environment variable and setting from `.env.example` (database, cache, secrets, LLM providers, literature APIs, data directory, model routing). |
| [Development](development.md) | Local development workflow: repo layout, running the stack, migrations, tests, linting, the layering convention, and the branch-per-feature Git workflow. |
| [Deployment](deployment.md) | Production deployment with Docker Compose: the prod overlay, bind-mount data directories, restricted-network build args, migrations, ports, and backups. |

## Where things live

- Source code: `src/backend/` (FastAPI app package `app`, ARQ worker package `worker`) and
  `src/frontend/` (React + Vite).
- Docker configuration: `docker/` (base compose plus dev and prod overlays, per-service Dockerfiles,
  nginx config).
- These docs: `docs/`.
