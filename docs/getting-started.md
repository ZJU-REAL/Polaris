# Getting started

This guide gets Polaris running on your machine and through your first login. The recommended path
uses Docker and needs nothing else installed. A no-Docker local path is also documented for focused
backend or frontend development. For production deployment, see [Deployment](deployment.md); for
the full environment variable reference, see [Configuration](configuration.md).

## Prerequisites check

The Docker path (recommended) needs:

- **Docker Engine 24+** and **Docker Compose v2** (the `docker compose` subcommand, not the legacy
  `docker-compose` binary). Check with `docker --version` and `docker compose version`.
- **Free host ports**: `5173` (Vite dev server), `8000` (API, configurable via `POLARIS_API_PORT`),
  and `8080` (nginx frontend in production mode, configurable via `POLARIS_FRONTEND_PORT`).
- **Disk**: the images are large — the shared TeX base image alone (tectonic + TeX Live + CJK
  fonts) is several GB. Budget roughly 20 GB for images plus data volumes.
- **RAM**: the full stack runs five services (Postgres, Redis, API, worker, frontend); 8 GB is a
  comfortable minimum.
- **Network**: the first build downloads the tectonic binary and a font pack from GitHub, apt
  packages, and pip wheels. On networks that cannot reach these directly, see the mirror build
  arguments in [Deployment](deployment.md#restricted-networks) and the first row of
  [common first-run errors](#common-first-run-errors) below.

The no-Docker path additionally needs:

- Python 3.12 or newer
- Node.js 18 or newer (with npm)

> [!TIP]
> The Docker path needs no local Python, Node, PostgreSQL, or Redis. Everything, including the
> database and cache, runs in containers with hot reload.

## 1. Clone

```bash
git clone https://github.com/ZJU-REAL/Polaris.git polaris
cd polaris
```

## 2. Configure `.env`

Copy the example file and edit the values you need:

```bash
cp .env.example .env
```

At minimum, set the following before your first real run:

| Key | Why |
| --- | --- |
| `POLARIS_SECRET_KEY` | Signs JWT auth tokens. Generate one with `openssl rand -hex 32`. |
| `POLARIS_ENCRYPTION_KEY` | Fernet key that encrypts SSH credentials at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. In dev you may leave it **empty** (a key is derived from the secret key), but do not leave the template placeholder in place — it is not a valid Fernet key and saving an SSH credential will fail. |
| `POLARIS_INVITE_CODE` | The static fallback invite code used to register. Defaults to `polaris-lab`. Admins can later create managed registration codes in the app. |
| `POLARIS_OPENAI_COMPAT_API_KEY` and/or `POLARIS_ANTHROPIC_API_KEY` | At least one LLM provider key. The OpenAI-compatible base URL defaults to DeepSeek; point `POLARIS_OPENAI_COMPAT_BASE_URL` at whatever OpenAI-compatible endpoint you use. |

Optional but commonly set: `POLARIS_S2_API_KEY` (Semantic Scholar, higher rate limits) and
`POLARIS_OUTBOUND_PROXY` (for reaching arXiv / Semantic Scholar / OpenAlex when direct access is
unreliable; from inside Docker use `http://host.docker.internal:<port>`). See
[Configuration](configuration.md) for the full reference.

> [!NOTE]
> LLM provider keys and the model routing table can also be edited later from the admin panel in
> the running app. The `.env` values are just the initial seed.

## 3. Run the full stack (Docker)

```bash
make dev
```

The first run does two heavy things: it builds the shared TeX base image (`make texbase`, cached
afterwards and only rebuilt when `docker/Dockerfile.texbase` changes), then builds and starts every
service — PostgreSQL with pgvector, Redis, the API, the ARQ worker, and the frontend — with hot
reload and source bind-mounts.

**Then apply the database migrations** (required: Postgres tables are not auto-created; only the
no-Docker SQLite path creates its schema on startup):

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml \
  exec api alembic upgrade head
```

Once it is up:

- Frontend: <http://localhost:5173>
- Backend API docs (OpenAPI / Swagger UI): <http://localhost:8000/docs>

To follow logs or stop the stack:

```bash
make logs    # tail all services
make down    # stop and remove containers
```

## 4. First login

1. Open the frontend at <http://localhost:5173>.
2. Register a user with the invite code from `POLARIS_INVITE_CODE` (default `polaris-lab`). If you
   have not configured SMTP, registration asks for no email verification code.
3. **The first account to register is automatically promoted to platform administrator.**

## 5. What to do after first login

Work through these in order; each unlocks the next.

1. **Configure LLM providers and routing** — go to **Admin → LLM admin** (`/admin?tab=llm`). The
   `.env` keys seed the initial provider entries; here you can add providers, list their models,
   and edit the model routing table that maps each research stage to a provider, model, and
   optional reasoning effort. Users can override routes for themselves under **Settings → My
   LLM**. Until at least one route resolves, AI features return `LLM_NOT_CONFIGURED`.
2. **Create registration codes for your lab** — **Admin → Codes** (`/admin?tab=codes`). Managed
   codes can carry an expiry, a maximum number of uses, and preset research directions (a project
   is created automatically for the new user). The static `POLARIS_INVITE_CODE` keeps working as a
   fallback so you can never lock yourself out.
3. **Connect an SSH server** (needed for the experiment stage) — **Settings → SSH credentials**
   (`/settings?tab=ssh`). Add a host and key, then use **Test connection**; credentials are
   encrypted at rest with the Fernet key. Admin-side experiment policy (command allow/deny lists,
   budgets) lives under **Admin → Experiments**.
4. **Create your first direction library** — go to **Libraries** (`/libraries`) and create one. A
   structured AI interview helps you write the inclusion config (statement, goals, scope,
   exclusions), and running the ingest builds the corpus: candidate search, citation snowballing,
   relevance scoring, full-text extraction, and wiki compilation, all as one resumable task.
5. **Create a topic and link libraries** — create a project (`/projects/new`), then link one or
   more direction libraries to it. A topic holds no papers of its own; its corpus is the union of
   the libraries linked to it. From there, work through the pipeline stage by stage — see
   [Core concepts](concepts.md).
6. **Optional: enable PolarisBuddy** — the in-app assistant's multi-turn tool loop is off by
   default (it re-sends history and tool schemas every round, so it costs more than one-shot
   chat). Set `POLARIS_CHAT_AGENT_ENABLED=1` in `.env` and restart to enable it.
7. **Optional: configure the daily arXiv feed** — **Admin → Daily papers** sets the subscribed
   arXiv categories and the daily fetch time.

<!-- screenshot: Admin → LLM admin, the model routing table -->

## Common first-run errors

| Symptom | Cause and fix |
| --- | --- |
| `make dev` fails while building `polaris-texbase` (GitHub download stalls or apt is slow) | The TeX base image downloads the tectonic binary and a CJK font pack from GitHub and runs a large apt install. On restricted networks pass mirrors: `GITHUB_PROXY=https://gh-proxy.com/ APT_MIRROR=repo.huaweicloud.com make texbase`, then re-run `make dev`. For slow PyPI, pass `PIP_INDEX_URL` to the compose build. See [Deployment](deployment.md#restricted-networks). |
| `port is already allocated` on startup | Another service holds 5173, 8000, or 8080. Set `POLARIS_API_PORT` / `POLARIS_FRONTEND_PORT` and pass them to compose (export them, or run compose with `--env-file .env` — the Makefile does not pass it, and compose interpolation reads `.env` next to the compose file, not the repo root). Port 5173 is fixed in the dev overlay. |
| API is up but every page errors; logs show `relation "..." does not exist` | Migrations were not applied. Run the `alembic upgrade head` command from step 3. Needed again after any update that ships new migrations. |
| AI features return `LLM_NOT_CONFIGURED` (HTTP 503) | No LLM provider key is set, or the routing table has no usable route. Set a key in `.env` (then restart) or configure providers and routes in **Admin → LLM admin**. |
| Saving an SSH credential fails with a server error mentioning Fernet | `POLARIS_ENCRYPTION_KEY` still holds the `.env.example` placeholder, which is not a valid Fernet key. Set a real key (see step 2) or leave it empty in dev. Note that changing the key later makes previously stored credentials undecryptable. |
| Literature ingest finds nothing / arXiv, Semantic Scholar, or OpenAlex time out | Direct access to the literature APIs is blocked or flaky on your network. Set `POLARIS_OUTBOUND_PROXY` (e.g. `http://host.docker.internal:7897` for a proxy on the Docker host) and restart. |
| You edited worker code but behavior did not change | Under the dev overlay the worker's `arq --watch` only reloads the settings module. Run `docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml restart worker`. |

## No-Docker local path

For focused backend or frontend work you can run each side directly. The backend falls back to a
local SQLite database when `POLARIS_DATABASE_URL` is not set to Postgres (and creates its tables on
startup), so no external database is required for a quick start.

Backend (creates a virtualenv on first `make venv`, then runs uvicorn on port 8000):

```bash
make venv          # one-time: create src/backend/.venv and install deps
make backend-dev   # uvicorn app.main:app --reload --port 8000
```

Frontend (installs deps and runs the Vite dev server on port 5173):

```bash
make frontend-dev  # npm install && npm run dev
```

> [!WARNING]
> The Experiment Lab connects to real GPU servers over SSH and runs generated code there. Remote
> writes pass through human approval gates and command allow/deny lists, but you should still point
> Polaris only at machines you control and review the audit log.

## Next steps

- [Introduction](index.md): what each pipeline stage does, with links to the per-stage guides.
- [Core concepts](concepts.md): the pipeline, Voyages, skills, and the MCP tools.
- [Configuration](configuration.md): full environment variable reference.
- [Development](development.md): local workflow, migrations, tests, and the Git conventions.
- [Deployment](deployment.md): running Polaris in production.
