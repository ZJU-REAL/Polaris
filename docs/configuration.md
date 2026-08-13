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
| `POLARIS_ENV` | Runtime environment. `prod` forces safe defaults (the fake LLM fallback is structurally disabled, CORS is restricted). | `dev` (or `prod`) |
| `POLARIS_SECRET_KEY` | Signs JWT auth tokens. Generate with `openssl rand -hex 32`. | `change-me-random-64-chars` |
| `POLARIS_ENCRYPTION_KEY` | Fernet key that encrypts SSH credentials at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Empty in dev derives a key from the secret key; set it explicitly for production. Do not leave the template placeholder — it is not a valid Fernet key. | `change-me-fernet-key` |
| `POLARIS_INVITE_CODE` | Static fallback invite code for registration. Admin-managed registration codes (with expiry, usage limits, and preset directions) are created in the app under Admin → Codes; this static code always keeps working so the admin cannot be locked out. | `polaris-lab` |
| `POLARIS_SESSION_LIFETIME_SECONDS` | Login session lifetime in seconds. There is no refresh-token mechanism, so the default is long. | `2592000` (30 days) |
| `POLARIS_CORS_ORIGINS` | Extra allowed cross-origin frontend origins in `prod`, comma-separated. Only needed when the frontend is served from a different domain than the API; the web production path (nginx same-origin reverse proxy) does not need it, and the desktop client's `app://polaris` origin is always whitelisted. | (empty), e.g. `https://polaris.example.edu` |
| `POLARIS_LLM_FAKE_FALLBACK` | Fall back to the built-in fake LLM provider when no route is configured (key-less demos and tests only). Off by default; when off, AI features return `LLM_NOT_CONFIGURED` instead of fabricated content. Ignored (forced off) when `POLARIS_ENV=prod`. | (unset) |
| `POLARIS_CHAT_AGENT_ENABLED` | Enable PolarisBuddy's multi-turn tool loop. Off by default because each round re-sends the conversation history and tool schemas, which costs far more than one-shot chat. | (unset), set `1` to enable |
| `POLARIS_GITHUB_TOKEN` | GitHub PAT (repo scope) used by the in-app feedback feature to file issues. Empty disables issue creation (feedback still produces a draft). | (empty) |
| `POLARIS_GITHUB_REPO` | Target `owner/name` repository for feedback issues. | `ZJU-REAL/Polaris` |
| `POLARIS_PUBLIC_BASE_URL` | Public server root used to build stdio MCP download links. HTTP MCP always reuses the origin of its current `/mcp` request and ignores this setting. | (empty), for example, `https://polaris.example.edu` |
| `POLARIS_MCP_DOWNLOAD_LINK_TTL_SECONDS` | Lifetime of signed paper-figure download links, in seconds. Values are limited to 60 seconds through 24 hours. | `900` |
| `POLARIS_DATABASE_URL` | Async SQLAlchemy database URL. Falls back to local SQLite when unset, which enables a no-Docker quick start; production uses Postgres with asyncpg. | `postgresql+asyncpg://polaris:polaris@postgres:5432/polaris` (default when unset: `sqlite+aiosqlite:///./polaris_dev.db`) |
| `POLARIS_DB_POOL_SIZE` / `POLARIS_DB_MAX_OVERFLOW` / `POLARIS_DB_POOL_TIMEOUT` | SQLAlchemy connection pool sizing. The defaults are tuned for the worker's concurrency (parallel scoring sessions per voyage); shrink them only if your Postgres `max_connections` is low. | `20` / `50` / `30` |
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

## Email (`POLARIS_SMTP_*`)

Email powers registration verification codes and password reset. **Leaving `POLARIS_SMTP_HOST`
empty disables email entirely**: registration asks for no verification code and the login page
hides "forgot password" (the frontend reads `/api/auth/capabilities`). Configure it only if you
want those flows.

| Variable | Purpose | Default / example |
| --- | --- | --- |
| `POLARIS_SMTP_HOST` | SMTP server hostname. Empty disables email. | (empty), e.g. `smtp.zju.edu.cn` |
| `POLARIS_SMTP_PORT` | SMTP port. | `465` (ZJU mail uses `994`) |
| `POLARIS_SMTP_SECURITY` | `ssl` = TLS from the start (465/994), `starttls` = plain connection upgraded (587/25), `none` = unencrypted (intranet debugging only). | `ssl` |
| `POLARIS_SMTP_USER` | Login account. | (empty) |
| `POLARIS_SMTP_PASSWORD` | The mailbox's app password / authorization code — not the login password. | (empty) |
| `POLARIS_SMTP_FROM` | Sender address; falls back to `POLARIS_SMTP_USER` when empty. | (empty) |
| `POLARIS_SMTP_FROM_NAME` | Sender display name. | `Polaris` |
| `POLARIS_SMTP_TIMEOUT` | Send timeout in seconds. | `20` |

## Compose interpolation variables (repo-root `.env`, no prefix parsing)

These are read by Docker Compose itself (variable interpolation in `docker/docker-compose.yml`),
not by the application. Compose looks for the interpolation `.env` next to the compose file by
default, so pass `--env-file .env` from the repo root (or export the variables) for them to take
effect — see the note in [Deployment](deployment.md#deploy-from-published-images-no-build).

| Variable | Purpose | Default |
| --- | --- | --- |
| `POLARIS_API_PORT` | Host port for the API service. | `8000` |
| `POLARIS_FRONTEND_PORT` | Host port for the nginx frontend. | `8080` |
| `POLARIS_IMAGE_PREFIX` | Registry namespace for the `polaris-{api,worker,frontend}` images (both the local build tag and the pull source). | `tricktreat` |
| `POLARIS_IMAGE_TAG` | Image tag to build or pull. | `latest` |

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
| `GITHUB_PROXY` | Build arg (`make texbase` only): prefix to accelerate the TeX base image's GitHub downloads (tectonic binary, CJK font pack) on networks that cannot reach GitHub directly. | (empty), e.g. `https://gh-proxy.com/` |
| `APT_MIRROR` | Build arg (`make texbase` only): Debian mirror hostname for the TeX base image's apt installs. | (empty), e.g. `repo.huaweicloud.com` |
| `PIP_INDEX_URL` | Build arg: alternate PyPI mirror for the image build. | (empty), e.g. `https://pypi.tuna.tsinghua.edu.cn/simple` |
| `DEV_SRC` | Dev overlay only: source directory to bind-mount, so you can preview a branch from a dedicated worktree without touching `main`. | `..` (repo root) |

## MCP stdio variable

When running the external MCP server over stdio for a local desktop client
(`python -m app.mcp`), the caller is identified by an environment variable
rather than a JWT. Set `POLARIS_PUBLIC_BASE_URL` in the application settings
table when you need figure tools to return absolute download URLs.

| Variable | Purpose | Example |
| --- | --- | --- |
| `POLARIS_MCP_USER_EMAIL` | Email of a registered user the stdio MCP process acts as. | `you@example.com` |

## Model routing

Beyond the provider keys above, Polaris routes each research stage to a specific provider and model
through a DB-backed routing table, editable from the admin panel. This lets cheap models handle
scoring while strong models handle idea debate and paper drafting. All calls go through the single
`app/core/llm/` abstraction; see [Architecture](architecture.md#the-llm-abstraction-and-model-routing).

### Reasoning effort

Each route can also carry a **reasoning effort** — how much the model is allowed to think before
answering. Leave it unset (the default) and Polaris sends no effort parameter at all, so the model
uses its own default; existing routes are unaffected. Levels are `none`, `minimal`, `low`, `medium`,
`high`, `xhigh`, `max`, sent as `reasoning_effort` to OpenAI-compatible endpoints and as
`output_config.effort` to the native Anthropic API.

Support varies by model, and not every level is valid on every model that accepts the parameter —
a model may accept `low` but reject `minimal`. Polaris does not keep a per-model whitelist. If the
server rejects the request because of the effort parameter, the call is retried once without it and
a warning is logged, so an effort set on a model that cannot use it degrades to the model's default
instead of breaking the stage. Embedding and rerank routes have no effort setting.

Lower effort on high-volume mechanical stages (relevance scoring, extraction) cuts both cost and
latency; raise it on stages where correctness matters more than spend (self-verification, review).
