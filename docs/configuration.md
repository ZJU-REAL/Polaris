# Configuration

Polaris is configured through environment variables, read from `.env` at the repository root. Copy
the template and edit it:

```bash
cp .env.example .env
```

Application settings use the `POLARIS_` prefix (parsed by pydantic-settings). A few variables consumed
directly by the Postgres container image or by the Docker build do not use that prefix; they are
noted below.

## Application settings (`POLARIS_` prefix)

| Variable | Purpose | Default / example |
| --- | --- | --- |
| `POLARIS_ENV` | Runtime environment. | `dev` (or `prod`) |
| `POLARIS_SECRET_KEY` | Signs JWT auth tokens. Generate with `openssl rand -hex 32`. | `change-me-random-64-chars` |
| `POLARIS_ENCRYPTION_KEY` | Fernet key that encrypts SSH credentials at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Empty in dev derives a key from the secret key; set it explicitly for production. | `change-me-fernet-key` |
| `POLARIS_INVITE_CODE` | Invite code required to register a new user. | `polaris-lab` |
| `POLARIS_DATABASE_URL` | Async SQLAlchemy database URL. Falls back to local SQLite when unset, which enables a no-Docker quick start; production uses Postgres with asyncpg. | `postgresql+asyncpg://polaris:polaris@postgres:5432/polaris` (default when unset: `sqlite+aiosqlite:///./polaris_dev.db`) |
| `POLARIS_REDIS_URL` | Redis URL for the ARQ broker and cache. | `redis://redis:6379/0` (local default `redis://localhost:6379/0`) |
| `POLARIS_OPENAI_COMPAT_BASE_URL` | Base URL of the OpenAI-compatible provider. | `https://api.deepseek.com/v1` |
| `POLARIS_OPENAI_COMPAT_API_KEY` | API key for the OpenAI-compatible provider. | (empty) |
| `POLARIS_ANTHROPIC_API_KEY` | API key for Anthropic. | (empty) |
| `POLARIS_S2_API_KEY` | Semantic Scholar API key. Optional; without it rate limits are stricter. | (empty) |
| `POLARIS_OPENALEX_MAILTO` | Contact email for the OpenAlex polite pool. | `polaris@example.org` |
| `POLARIS_DATA_DIR` | Directory for PDFs and generated artifacts. In containers this is set to `/srv/data` and bind-mounted; keep it out of the code tree. | `./data` (containers: `/srv/data`) |
| `POLARIS_OUTBOUND_PROXY` | HTTP proxy for outbound literature API calls (arXiv, Semantic Scholar, OpenAlex) when direct access is unreliable. Not used for LLM or internal traffic. From inside Docker, reach a host proxy via `host.docker.internal`. | (empty), e.g. `http://host.docker.internal:7897` |
| `POLARIS_PIP_INDEX_URL` | Optional pip mirror used on the remote experiment servers. | (empty), e.g. `https://pypi.tuna.tsinghua.edu.cn/simple` |

> [!NOTE]
> LLM provider keys are the initial seed. The provider keys and the model routing table can also be
> managed from the admin panel once the app is running.

## Postgres container variables (no prefix)

These are read by the `pgvector/pgvector` image to initialize the database, and must match the
credentials in `POLARIS_DATABASE_URL`.

| Variable | Purpose | Default / example |
| --- | --- | --- |
| `POSTGRES_USER` | Database superuser created on first init. | `polaris` |
| `POSTGRES_PASSWORD` | Password for that user. | `polaris` |
| `POSTGRES_DB` | Database name created on first init. | `polaris` |

## Build-time and deployment variables

Set these when invoking Docker Compose (not in `.env`); see [Deployment](deployment.md).

| Variable | Purpose | Default / example |
| --- | --- | --- |
| `GITHUB_PROXY` | Build arg: prefix to accelerate tectonic's GitHub downloads on networks that cannot reach GitHub directly. | (empty), e.g. `https://gh-proxy.com/` |
| `PIP_INDEX_URL` | Build arg: alternate PyPI mirror for the image build. | (empty), e.g. `https://pypi.tuna.tsinghua.edu.cn/simple` |
| `DEV_SRC` | Dev overlay only: source directory to bind-mount, so you can preview a branch from a dedicated worktree without touching `main`. | `..` (repo root) |

## MCP stdio variable

When running the external MCP server over stdio for a local desktop client
(`python -m app.mcp`), the caller is identified by an environment variable rather than a JWT.

| Variable | Purpose | Example |
| --- | --- | --- |
| `POLARIS_MCP_USER_EMAIL` | Email of a registered user the stdio MCP process acts as. | `you@example.com` |

## Model routing

Beyond the provider keys above, Polaris routes each research stage to a specific provider and model
through a DB-backed routing table, editable from the admin panel. This lets cheap models handle
scoring while strong models handle idea debate and paper drafting. All calls go through the single
`app/core/llm/` abstraction; see [Architecture](architecture.md#the-llm-abstraction-and-model-routing).
