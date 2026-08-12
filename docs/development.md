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
make migrate   # cd src/backend && alembic upgrade head (uses the local venv)
make test      # backend pytest + frontend build
make lint      # ruff check (backend) + tsc --noEmit (frontend and desktop)
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
[Core Concepts](concepts.md#the-mcp-read-only-tool-layer) and the user-facing guide in [MCP](mcp.md).

Modules are grouped by what they read: `literature.py` and `knowledge.py` (papers, chunks, concepts,
graph), `agentic_search.py` (cheap wide scans and full-text grep), `figures.py` (images),
`external.py` (third-party APIs), `project_state.py` (ideas, experiments, fact packs),
`workspace.py` (topic status, tasks, gates), `libraries.py` (direction libraries, daily pool),
`projects.py` (user-level topic discovery), and `writing.py` (manuscripts), plus the
assistant-oriented modules `plan.py` (conversation plans), `skills.py` (agent-skill loading),
`subagent.py` (delegated search), and `memory.py` (PolarisBuddy's per-user memory — `remember` is the
registry's one write tool and is filtered out of the external MCP list). A new tool goes in whichever
module already owns its subject, and gets registered by importing that module in
`app/tools/__init__.py`.

Two rules keep the layer honest. **Tools are thin wrappers over `services/*`** — no business logic
lives here, so REST and MCP can never drift apart. **Tools declare their scope in `ToolSpec`**:
`scope="project"` is the default and makes MCP require and authorize `project_id`;
`scope="user"` is reserved for authenticated-user discovery such as
`list_accessible_projects`. An id passed to a project-scoped tool must read as not found when it
belongs to another topic, even when the caller has access through a different topic.
`tests/test_mcp_workspace_tools.py` pins that for tasks, manuscripts, and libraries; a new tool that
takes an id needs the same case.

### Testing the tools

Tool handlers call into `services/*`, so a refactor there (changed signature, dropped field, new
table layout) does not break imports — it breaks the tool at call time. Two endpoints exist to catch
that, both routed through `app/mcp/dispatch.call_tool`, the exact path external MCP clients take:

| Endpoint | What it does |
| --- | --- |
| `POST /api/mcp/tools/{name}/invoke` | Runs one tool with arguments you supply and returns the raw MCP content blocks (text plus inline images), so you see what a client would see. |
| `POST /api/mcp/selfcheck` | Collects sample data from a topic (a paper, a figure, a concept, an idea, an experiment, a manuscript), fills each tool's required arguments, and runs the whole catalog, reporting `ok` / `error` / `skipped` per tool. |

Both live behind the **Settings → MCP** page, which has two views:

- **Tool catalog** — every tool as a card, badged with its self-check result, each with an inline
  "try it" panel prefilled with the self-check's sample arguments.
- **Playground** — a searchable tool list plus a full debugging panel: a schema-generated argument
  form or hand-written JSON, the response rendered exactly as a client receives it (text and inline
  images), the equivalent JSON-RPC `tools/call` request (copyable, to replay from your own client),
  and this session's call history, where clicking an entry replays its arguments and result.

Sample arguments are chosen to keep a self-check cheap and deterministic: filter-style optional
parameters are left unset, `mode` is pinned to `keyword` so no embedding call is made, and figure
tools render at 512 px. Network tools are skipped unless `include_network` is set, since they really
call Semantic Scholar and OpenAlex. A tool whose required argument has no sample in that topic (no
manuscript yet, no extracted figures) is reported as `skipped`, not as a failure.

The sampler recognises arguments by name (`app/mcp/selfcheck.py::_sample_value`): `paper_id`,
`library_id`, `task_id`, `idea_id`, `experiment_id`, `manuscript_id`, `path`, `index`, `query`/`q`,
`name`, `doi`. **A new tool whose required argument is not in that list will silently be reported as
`skipped` forever** — add the sample to `collect_samples` and a reason to `_MISSING_REASON` in the
same change, or the self-check will quietly stop covering it.

`search_papers`, `search_chunks`, and `find_figures` all take `mode` (`keyword` | `semantic`), and
all three fall back to keyword search when the embedding call fails for any reason — the embedding
service being unreachable degrades result quality, it does not take the tool down. Without that
fallback a dead embedding host turned into a ~48s retry storm and a red self-check.

`tests/test_mcp_server.py::test_selfcheck` runs the same self-check against the test fixtures and
fails if any tool reports `error`, so tool rot surfaces in CI rather than in a client.
