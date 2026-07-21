# Development

This is the practical guide for working on Polaris locally. For first-time setup, see
[Getting Started](getting-started.md); for the design behind the code, see
[Architecture](architecture.md).

## Repository layout

```text
src/
  backend/           FastAPI app + ARQ worker (Python 3.12)
    app/
      api/           thin FastAPI routers (no business logic)
      services/      business logic (ingest, wiki, ideas, review, experiments, manuscripts, skills, ...)
      models/        SQLAlchemy 2 models
      schemas/       Pydantic v2 request/response models
      agents/
        voyage/      the Voyage engine: navigator, helm, sextant, checks, tool_loop, per-domain actions
      core/          config, db, redis, queue (ARQ), events (SSE), security (Fernet), llm/ abstraction
      tools/         read-only tool registry (shared by the agent loop and the MCP server)
      mcp/           external MCP server (Streamable HTTP and stdio)
    worker/          ARQ worker
    tests/
  frontend/          React 18 + TypeScript 5 + Vite 5
    src/features/    one folder per product area: wiki, reading, forge, review, experiment,
                     writer, paper-review, voyages, skills, mcp, settings, ...
docker/              Dockerfiles and compose (base, dev overlay, prod overlay), nginx config
docs/                this documentation
```

## Running the stack

The fastest way to run everything with hot reload is Docker:

```bash
make dev     # full stack via docker compose (dev overlay), hot reload
make logs    # tail all services
make down    # stop and remove containers
```

- Frontend: <http://localhost:5173>
- Backend API docs: <http://localhost:8000/docs>

## Running backend or frontend standalone

For focused work you can run each side without Docker. The backend falls back to SQLite when
`POLARIS_DATABASE_URL` is not pointed at Postgres, so no external database is required.

```bash
make venv          # one-time: create src/backend/.venv and install deps (editable, with dev extras)
make backend-dev   # uvicorn app.main:app --reload --port 8000
make frontend-dev  # npm install && vite dev on :5173
```

> [!NOTE]
> Under the dev overlay, the worker uses `arq --watch`, which only reloads the settings module.
> Modules already imported under `app/` are not refreshed, so run `docker compose restart worker`
> after editing worker code.

## Migrations, tests, and linting

```bash
make migrate   # cd src/backend && alembic upgrade head
make test      # backend pytest + frontend build
make lint      # ruff check (backend) + tsc --noEmit (frontend)
make build     # build production images
```

## Layering convention

The backend follows one strict rule, and reviews enforce it:

- `api/` routers are thin and hold no business logic.
- `services/` hold the business logic and never import FastAPI.
- `models/` hold SQLAlchemy models.
- **All LLM calls go through `app/core/llm/`.** No direct provider SDK imports in business code; model
  choice comes from the DB routing table.
- **Deterministic vs. judgemental split.** Crawling, parsing, deduplication, and watermark logic are
  ordinary code or worker tasks; only judgement calls (scoring, synthesis, generation) reach an LLM.
- **Long tasks go through the ARQ worker**, never in the request thread. Complex multi-step tasks use
  the Voyage engine (Navigator plans, Helm executes, Sextant verifies) with a persistent state
  machine; nodes that need a human create a gate and pause until approved.
- **Secrets are encrypted at rest** with Fernet (`app/core/security.py`); no secrets in logs; every
  remote write is gated.

Frontend conventions: TypeScript strict, function components with hooks, all server state through
TanStack Query (no hand-written fetch-in-useEffect), and design tokens in
`src/styles/tokens.css` (no hard-coded colors in components).

## Adding a migration

Generate migrations with a random revision id, never a hand-sequenced one:

```bash
cd src/backend
.venv/bin/alembic revision -m "describe the change"   # produces a random 12-char hex id
```

Hand-rolled "rolling" ids collide when two parallel branches each create "the next migration." Before
merging, confirm the migration's `down_revision` chains onto the latest head on `origin/main`, and run
an `alembic upgrade head` plus a downgrade round-trip to confirm a single head with no duplicate table
creation.

## Git workflow

The full rules live in the project's Git workflow guide; the essentials:

- **`main` is a read-only mirror of `origin/main`.** Only ever fast-forward it (`git pull --ff-only`).
  Never merge a feature branch into `main` and never commit on `main`.
- **One feature = one branch = one worktree = one pull request**, always branched from the latest
  `origin/main`:

  ```bash
  git fetch origin
  git worktree add ../wt/feat-x -b feat/x origin/main
  # develop, commit, push
  gh pr create --draft
  # after merge
  git worktree remove ../wt/feat-x && git branch -d feat/x
  ```

- **Catch up on main with `git rebase origin/main`, never `git merge main`** into a feature branch (a
  merge drags unrelated features into your diff). Force-push a rebased branch with
  `--force-with-lease`.
- **Commits, PRs, and issues are in English.** Use conventional commits
  (`feat/fix/chore/docs/refactor(scope): ...`). File the issue first and link the PR with
  `Closes #N`.
- **Production deploys only from `origin/main`,** never from a local branch.

## The MCP tools during development

The read-only tool registry in `app/tools/` is the single source of truth for retrieval tools, used
both by the internal agent loop and by the external MCP server in `app/mcp/`. Adding a tool is a
single handler in `app/tools/`; it then becomes visible to both consumers. See the tool layer in
[Core Concepts](concepts.md#the-mcp-read-only-tool-layer).
