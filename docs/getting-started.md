# Getting Started

This guide gets Polaris running on your machine. The recommended path uses Docker and needs nothing
else installed. A no-Docker local path is also documented for backend and frontend development.

## Prerequisites

The Docker path (recommended) needs only:

- Docker (Engine 24+ recommended)
- Docker Compose v2 (the `docker compose` subcommand, not the legacy `docker-compose` binary)

The no-Docker path additionally needs:

- Python 3.12 or newer
- Node.js 18 or newer (with npm)

> [!TIP]
> The Docker path needs no local Python, Node, PostgreSQL, or Redis. Everything, including the
> database and cache, runs in containers with hot reload.

## 1. Clone

```bash
git clone <your-fork-or-remote-url> polaris
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
| `POLARIS_ENCRYPTION_KEY` | Fernet key that encrypts SSH credentials at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. In dev it is derived from the secret key if left empty, but set it explicitly for anything real. |
| `POLARIS_INVITE_CODE` | The invite code lab members use to register. Defaults to `polaris-lab`. |
| `POLARIS_OPENAI_COMPAT_API_KEY` and/or `POLARIS_ANTHROPIC_API_KEY` | At least one LLM provider key. The OpenAI-compatible base URL defaults to DeepSeek; point it at whatever OpenAI-compatible endpoint you use. |

Optional but commonly set: `POLARIS_S2_API_KEY` (Semantic Scholar, higher rate limits) and
`POLARIS_OUTBOUND_PROXY` (for reaching arXiv / Semantic Scholar / OpenAlex when direct access is
unreliable). See [Configuration](configuration.md) for the full reference.

> [!NOTE]
> LLM provider keys and the model routing table can also be edited later from the admin panel in the
> running app. The `.env` values are just the initial seed.

## 3. Run the full stack (Docker)

```bash
make dev
```

This builds and starts every service (PostgreSQL with pgvector, Redis, the API, the ARQ worker, and
the frontend) with hot reload and source bind-mounts.

Once it is up:

- Frontend: <http://localhost:5173>
- Backend API docs (OpenAPI / Swagger UI): <http://localhost:8000/docs>

To follow logs or stop the stack:

```bash
make logs    # tail all services
make down    # stop and remove containers
```

## 4. First run

1. Open the frontend at <http://localhost:5173>.
2. Register a user with the invite code you set in `POLARIS_INVITE_CODE`.
3. The first account to register becomes the initial administrator, who can then manage users, the
   LLM model routing table, and other settings.

From there, create a project (research direction) and start with the literature stage. The
[Core Concepts](concepts.md) document explains the pipeline you will work through.

## No-Docker local path

For focused backend or frontend work you can run each side directly. The backend falls back to a
local SQLite database when `POLARIS_DATABASE_URL` is not set to Postgres, so no external database is
required for a quick start.

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

- [Core Concepts](concepts.md): understand the pipeline, Voyages, skills, and the MCP tools.
- [Configuration](configuration.md): full environment variable reference.
- [Development](development.md): local workflow, migrations, tests, and the Git conventions.
- [Deployment](deployment.md): running Polaris in production.
