<p align="center">
  <img src="docs/assets/polaris-logo.svg" alt="Polaris" width="440">
</p>

<p align="center">
  <strong>Autonomous, end-to-end AI research: from literature to a reviewed paper.</strong><br>
  Powered by a long-running agent core that plans, executes, and self-verifies its own work, turning every task into a resumable, auditable, human-gated run.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/AI_Scientist-7438F0?style=flat-square&logo=data:image/svg%2bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0id2hpdGUiPjxwYXRoIGQ9Ik0xMiAxQzEzLjIgNyAxNSA4LjggMjIgMTIgMTUgMTUuMiAxMy4yIDE3IDEyIDIzIDEwLjggMTcgOSAxNS4yIDIgMTIgOSA4LjggMTAuOCA3IDEyIDFaIi8+PC9zdmc+&logoColor=white" alt="AI Scientist">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue?style=flat-square" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker Compose">
</p>

<p align="center">
  <img src="docs/assets/polaris-banner.jpg" alt="Polaris: from literature to a reviewed paper" width="100%">
</p>

---

Polaris runs the entire research lifecycle as a single web application: literature survey, idea
generation, idea review, experiment building on real GPU servers, LaTeX paper writing, and paper
review. It is built for a research lab, with multi-user access, RBAC, and invite-code registration, and
it treats every long task as a **Voyage**: a persisted, resumable, human-gated agent run that can span
hours or days without losing state.

> [!NOTE]
> Polaris is not a chatbot wrapper. The heavy lifting (crawling, parsing, deduplication, metric parsing,
> citation matching) is deterministic code. LLMs are reserved for the judgement calls: scoring,
> synthesis, drafting, and review. This split keeps runs cheap, reproducible, and auditable.

## The research pipeline

Polaris models research as six stages. Each stage produces durable artifacts that the next stage
consumes, and every hand-off can pause at a human approval gate.

```mermaid
flowchart LR
    L["Literature<br/>Research Wiki"]
    I["Idea<br/>Idea Forge"]
    R["Idea Review<br/>Elo debate"]
    X["Experiment<br/>GPU / SSH"]
    W["Paper Writing<br/>LaTeX"]
    V["Paper Review<br/>Citation check"]
    S(["Submission"])

    L --> I --> R
    R -->|promotion gate| X
    X --> W --> V
    V -->|submission gate| S

    classDef stage fill:#eaf1ff,stroke:#2f6bff,stroke-width:1px,color:#10233f;
    classDef gate fill:#fff3e0,stroke:#f59e0b,stroke-width:1px,color:#5b3b00;
    class L,I,R,X,W,V stage;
    class S gate;
```

| Stage | What Polaris actually does |
| --- | --- |
| **Literature** | The Research Wiki ingests papers from OpenAlex, Semantic Scholar, and arXiv. Cold start snowballs citations from anchor papers, scores relevance against a project rubric, extracts full text (PyMuPDF), and compiles each paper into a cross-linked wiki page (TL;DR, method, reusable ideas, concept backlinks). Incremental daily sync with watermark resume; pgvector semantic search; one-click Obsidian vault export. |
| **Idea** | Idea Forge runs multi-signal gap analysis over the knowledge base (concept co-occurrence holes, extracted paper limitations, trend velocity, survey gaps) to drive retrieval-planned idea generation. Ideas are scored on four axes (novelty, feasibility, operability, impact), deduplicated semantically, and funneled to a candidate pool. A deep Research Proposal builder then hardens the winner with a plan-execute-verify loop. |
| **Idea Review** | Configurable-persona reviewer agents debate pairwise; a judge produces an Elo tournament ranking. Lab members join the discussion live over WebSocket, and their comments enter the agent context as first-class input. |
| **Experiment** | The Experiment Lab uses per-user, Fernet-encrypted SSH credentials to reach the lab's GPU servers. An experiment Voyage plans the study, passes a compute-budget gate, writes code, runs a smoke test, launches runs with streamed logs and live metric curves, then auto-iterates: parse metrics, reflect, then improve, debug, or stop. Figures are generated and VLM-checked. |
| **Paper Writing** | The Paper Writer opens a multi-file LaTeX project (NeurIPS, ICLR, ACL templates) with a CodeMirror 6 editor, real-time collaborative editing (CRDT), and server-side tectonic compilation to a live PDF preview. An agent drafts section by section, but experiment numbers may only come from real `ExperimentRun` metrics and citations must map to real knowledge-base entries. |
| **Paper Review** | Line-by-line citation verification (existence: exact, minor, or fabricated; support: supported, partial, or unsupported) plus deterministic fact-checking of every number against the experiment record, then multi-perspective top-venue reviewer agents and a meta-review. A fabricated citation forces a non-pass. |

## The Voyage agent core

Research tasks are long-running by nature: a cold-start literature backfill takes hours, an experiment
runs for days. Polaris's central abstraction is that every complex task is a Voyage: a resumable,
auditable run driven by a persisted three-part loop.

| Component | Role |
| --- | --- |
| **Navigator** | Planning. Decomposes a goal into a step plan with sub-goals, dependencies, and budget. In loop mode it edits the plan incrementally as evidence arrives, rather than replanning from scratch. |
| **Helm** | Execution. Runs a single step (LLM calls, tool calls, SSH remote ops, literature-API queries) and returns an observation. |
| **Sextant** | Self-verification. Checks each step against structured acceptance criteria (exit code, artifact exists, schema valid, metric threshold, count, LLM rubric). Deterministic checks run first; failures feed diagnostics back to Navigator, and repeated failure escalates to a human gate. |

> [!IMPORTANT]
> A Voyage is backed by a persistent state machine (`planning -> executing -> verifying -> ...`). If a
> worker crashes mid-run, the Voyage resumes from its last checkpoint after a health check. Budgets are
> attached to the run and auto-pause it when exceeded; every plan, action, and verdict is retained and
> replayable in the UI.

Not every task needs the full cognitive loop. A shared **Runtime** shell (state machine, checkpointing,
gates, budget, cancellation, event streaming) serves all task kinds, while the **Brain** (the full
plan-execute-verify loop) activates only for open-ended kinds such as experiments. Predictable pipelines
(wiki compile, idea review, paper drafting) run on fixed templates instead of being over-orchestrated.

## Key features

- **Research Wiki, "compile, don't retrieve."** LLMs read papers and compile a persistent, cross-linked
  knowledge base up front, instead of doing on-demand RAG at query time. Exports to an Obsidian vault
  with `[[wikilinks]]` and frontmatter.
- **Idea Forge.** Signal-driven gap analysis, four-axis scoring, semantic dedup, and a deep
  Research-Proposal builder with novelty double-checking against the library and external sources.
- **Multi-agent and human review.** Persona reviewer agents debate to an Elo ranking; humans join live
  and are injected into the agent context, not bolted on afterward.
- **Experiment Lab over SSH.** Agents write and run code on real GPU servers, iterate on metrics, and
  collect logs and figures, under gated remote writes, command allow/deny lists, full audit, and triple
  budget caps (total, per-run, concurrency).
- **Paper Writer.** Online multi-file LaTeX with collaborative CRDT editing and server-side tectonic
  compilation; agent drafting bound to real metrics and real citations.
- **Paper Review with citation verification.** Existence and support are checked per citation against the
  library, Semantic Scholar, and OpenAlex; numbers are fact-checked against the experiment record.
- **Skill system.** Agent behavior is packaged as data, not code: versionable, composable `guidance`,
  `rubric`, `persona`, and `workflow` packs injected at named points into agent prompts, with a
  publish-approve-install-rate marketplace. Each Voyage snapshots the skills it used for reproducibility.
- **MCP tool layer.** A single registry of read-only tools (literature, knowledge, project state,
  external search) is exposed both internally to the agent loop and externally as an **MCP server**
  (Streamable HTTP and stdio) for Claude Desktop and Cursor. Project-isolated and strictly read-only.
- **Real-time everywhere.** SSE for agent streaming and Voyage progress; WebSocket for review
  discussions, approval notifications, experiment log tracking, and collaborative editing.
- **Multi-user and RBAC.** JWT auth (fastapi-users), invite-code registration, role-based access, and
  per-call token/cost accounting attributed to user, project, and voyage.
- **LLM abstraction and model routing.** All model calls go through one layer; a DB-backed routing table
  maps each research stage to a provider and model (cheap models for scoring, strong models for debate
  and drafting), editable from an admin panel.

## Tech stack

| Layer | Technology |
| --- | --- |
| Frontend | React 18 + TypeScript 5 + Vite 5, TanStack Query for all server state, CodeMirror 6, Yjs (CRDT), react-pdf, KaTeX |
| Backend | FastAPI (fully async) + SQLAlchemy 2 + Alembic + fastapi-users (JWT) |
| Task queue | ARQ (Redis broker); every long task runs off the request thread |
| Data | PostgreSQL 16 with pgvector + Redis 7 |
| Remote execution | asyncssh to GPU servers; SSH keys encrypted at rest with Fernet |
| LaTeX | tectonic, server-side, with a cached macro volume |
| LLM | Multi-provider abstraction (OpenAI-compatible and Anthropic) with a DB model-routing table |
| Deployment | Docker Compose (postgres, redis, api, worker, frontend) |

## Quick start

> [!TIP]
> Docker Compose is the recommended way to run Polaris, in development and in production. It needs only
> Docker and Docker Compose installed, with no local Python, Node, or database. See
> [docs/deployment.md](docs/deployment.md) for production deployment.

```bash
cp .env.example .env        # set provider keys and secrets
make dev                    # full stack via docker compose, hot reload
```

- Frontend: <http://localhost:5173>
- Backend API docs: <http://localhost:8000/docs>

Local development without Docker (falls back to SQLite):

```bash
make backend-dev            # venv + uvicorn on :8000
make frontend-dev           # npm install + vite dev on :5173
```

Common tasks:

```bash
make migrate                # alembic upgrade head
make test                   # backend pytest + frontend build
make lint                   # ruff check + tsc --noEmit
```

## Docker deployment

Pre-built `amd64` images are published to Docker Hub as `tricktreat/polaris-{api,worker,frontend}`
(pushed by CI on every `v*` tag — see [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml)).
Deploy on any host with Docker, no build required:

```bash
# On the server, in a checkout (or just the docker/ dir + .env):
cp .env.example .env
# Edit .env:
#   - POLARIS_ENV=prod                      (forces safe prod defaults)
#   - POLARIS_IMAGE_TAG=v0.1.0              (or leave 'latest'; prefix defaults to tricktreat)
#   - POLARIS_SECRET_KEY / POLARIS_ENCRYPTION_KEY   (generate; see comments in .env.example)
#   - POLARIS_DATABASE_URL password must match POSTGRES_PASSWORD
#   - at least one LLM key (or leave blank and add it later in the admin UI)

docker compose --env-file .env -f docker/docker-compose.yml pull      # pull the published images
docker compose --env-file .env -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml exec api alembic upgrade head   # required on first run
```

The frontend is served at `http://<host>:8080` (nginx reverse-proxies `/api`, `/ws`, `/mcp` to the
api container). The same `docker/docker-compose.yml` both builds locally (`make build`) and pulls
published images — `image:` doubles as the build tag and the pull source, controlled by
`POLARIS_IMAGE_PREFIX` / `POLARIS_IMAGE_TAG` in `.env`.

> [!IMPORTANT]
> Pass `--env-file .env` so Compose reads `POLARIS_IMAGE_TAG` / `POLARIS_IMAGE_PREFIX` from the repo
> root `.env`. Without it, Compose looks for the interpolation `.env` next to the compose file
> (`docker/`) and silently falls back to `tricktreat/...:latest`.

> [!NOTE]
> The `worker` container is not optional — it runs every long task (literature fetch, AI generation,
> experiments, compilation). Postgres tables are not auto-created, so the first-run `alembic upgrade
> head` is mandatory. For host-path bind mounts, backups, and restricted-network builds, see
> [docs/deployment.md](docs/deployment.md).

## Documentation

Full documentation lives in [docs/](docs/):

- [Getting started](docs/getting-started.md): install, configure, and run Polaris
- [Architecture](docs/architecture.md): system design and the Voyage agent core
- [Concepts](docs/concepts.md): the research pipeline, Voyage, skills, and MCP tools
- [Deployment](docs/deployment.md): production deployment with Docker Compose
- [Configuration](docs/configuration.md): environment variables and settings
- [Development](docs/development.md): local workflow and conventions

## Repository layout

```text
src/
  backend/       FastAPI app (package: app) and ARQ worker (package: worker)
    app/
      api/         thin routers
      services/    business logic (ingest, wiki, ideas, review, experiments, manuscripts, ...)
      models/      SQLAlchemy models
      agents/voyage/  the Voyage engine (navigator, helm, sextant, tool loop, per-domain actions)
      core/        config, db, queue (ARQ), events (SSE), llm/ abstraction
      tools/, mcp/ read-only tool registry and the external MCP server
  frontend/      React + Vite (src/features/ has one folder per product area)
docker/          Dockerfiles and compose (base, dev override, prod overlay)
docs/            English project documentation
```

## Design principles

- **Strict layering.** Thin routers call services; services hold the business logic and never import the
  web framework; models sit underneath.
- **Deterministic vs. judgemental split.** Deterministic work (crawling, parsing, dedup) is plain code or
  worker tasks; only judgement calls reach an LLM.
- **One LLM boundary.** All model calls go through a single abstraction layer, and model choice comes from
  a database routing table rather than being hard-coded.

See [docs/architecture.md](docs/architecture.md) for the full design.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: one feature is one branch is one pull request, branched
from the latest `origin/main`, with English conventional-commit messages, and `main` stays a read-only
fast-forward mirror of `origin/main`.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.
