# Configuration

Most of Polaris is configured inside the app (Settings) and stored in its database. A small set of
values are environment variables read by the engine at startup. You rarely need them: the desktop
app starts its engine with working defaults and sets the database path and data folder itself.

Where the engine gets its environment:

- **Desktop app.** The engine inherits the app's environment. To set a variable, start the app from
  a terminal with that variable set (for example `POLARIS_OUTBOUND_PROXY=http://127.0.0.1:7897`
  before the app's command).
- **Running from source** (`make backend-dev`). The engine also reads a `.env` file from its working
  directory, `src/backend/`. Copy the repo-root template there and uncomment what you need:

  ```bash
  cp .env.example src/backend/.env
  ```

Application settings use the `POLARIS_` prefix (parsed by pydantic-settings).

The engine is one local process with a **SQLite** database, an in-process task queue, and an
in-process scheduler. It needs no external database, cache, or queue service, and there are no
settings for them.

## Configuration layering (#737)

Configuration lives in four layers. Each value has exactly one home; the layer answers "who owns
this and when can it change".

| Layer | Owner | What belongs here | Examples |
| --- | --- | --- | --- |
| Environment (`POLARIS_*`) | Whoever starts the engine | **Machine facts**: values that describe the computer and its surroundings, fixed before the process starts. | database path, secret keys, data dir, proxies |
| Kernel config tree + kernel KV (desktop SQLite) | The kernel | **Plugin composition and kernel behavior**: which plugins are loaded, their config, and settings that change what the kernel itself does. | plugin entries/enable state, plugin install records, market index endpoint (`PluginMetaStore` key `market:endpoint`) |
| Electron store (`userData/config.json`) | The desktop shell | **Shell-only preferences**: things only the window chrome cares about, meaningless to the kernel or backend. | window bounds/maximized state |
| Database | The application | **User and domain state**. Two sub-homes: *preferences* go to `users.settings` namespaced keys. They sit on the local user (`app/services/local_user.py`) and machine-level ones are read/written through `app/services/owner_settings.py`, which works without a request (the worker's cron reads them too); *platform-operational state* stays in `system_settings`. | preferences: `daily.sync_time`, `daily.retention_days`, `daily.sync_scope`, `tts.admin`, `affiliations.extraction_mode`, `daily.categories`, `daily.subscriptions`, `onboarding.dismissed`; operational: probe state, `claim_today` markers, relevance-anchor caches, active embedding space, LLM call-log switch, watchdog cap, experiment host facts, literature/document-processing docs (user-tunable fields share one atomic document with encrypted credential pools, so they conservatively stay put) |

Placement test, in order:

1. Known before the process starts, per-machine? → environment.
2. Does it configure what the kernel loads or how the kernel behaves? → kernel tree/KV.
3. Does only the desktop window chrome care? → electron store.
4. Is it the user's preference? → `users.settings` (namespaced key). Is it machine
   state, a derived cache, or an operator guardrail? → `system_settings`.

Migration notes: the backend's read fallback to the legacy `system_settings` rows is gone (#821).
Migration `d3f9a1c7e2b4` copies any legacy value the user didn't have yet onto the user, then
deletes the legacy rows, so preferences have one home. The desktop market endpoint still reads
through to the old electron-store value once (moving a non-default value into the kernel KV);
removing that could drop a custom endpoint for anyone who hasn't opened the market since
upgrading.

## Application settings (`POLARIS_` prefix)

| Variable | Purpose | Default / example |
| --- | --- | --- |
| `POLARIS_ENV` | Runtime environment. `prod` requires an explicit `POLARIS_ENCRYPTION_KEY`. It no longer affects CORS (see `POLARIS_CORS_ORIGINS`). | `dev` (or `prod`) |
| `POLARIS_SECRET_KEY` | Signs the local session tokens, signed download links, and browser-extension key digests. The desktop app generates one per install (`userData/engine-secrets.json`). When running from source, generate one with `openssl rand -hex 32`; the default is public. | `dev-only-secret-key-change-me` |
| `POLARIS_ENCRYPTION_KEY` | Fernet key that encrypts stored secrets (model API keys, SSH and connection credentials, MCP/agent environment variables, literature and MinerU keys). The desktop app generates one per install. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Empty derives a key from the secret key (not allowed with `POLARIS_ENV=prod`). Do not set a placeholder — it is not a valid Fernet key. Values encrypted with the key derived from the old default secret key are still readable and are re-encrypted with this key at startup; values encrypted with any other earlier key are not. | (empty) |
| `POLARIS_CORS_ORIGINS` | Extra origins (comma-separated, full origins) allowed to call the engine from a browser page. `app://polaris` is always allowed; `*` is never used. The Vite dev server proxies `/api` same-origin and does not need this. Set it when a page on another origin talks to the engine directly, for example `http://localhost:6274` for the MCP Inspector, or `http://localhost:5173` if you point the frontend at the engine without the proxy. Origins listed here may also obtain a local session when `POLARIS_LOCAL_SESSION_SECRET` is unset. | (empty) |
| `POLARIS_ALLOWED_HOSTS` | Extra `Host` header names (comma-separated, any port) the engine accepts. `127.0.0.1`, `localhost`, and `[::1]` are always accepted; any other host gets `400`, which blocks DNS rebinding. `*` turns the check off — only for debugging setups that know why they need it. | (empty) |
| `POLARIS_LOCAL_SESSION_SECRET` | Set by the desktop app on every launch. When set, `POST /api/auth/local-session` only answers requests carrying the same value in `X-Polaris-Session-Secret`. When unset (running from source, tests), it answers requests with no `Origin` header or an allowed one. Do not set it by hand. | (set by the desktop app) |
| `POLARIS_INSTANCE_ID` | Set by the desktop app on every launch and echoed by `/api/health`, so the app can tell its own engine apart from another process on the same port. | (set by the desktop app) |
| `POLARIS_SESSION_LIFETIME_SECONDS` | Session lifetime in seconds. There is no refresh-token mechanism, so the default is long. | `2592000` (30 days) |
| `POLARIS_ACP_LLM_CONCURRENCY` | How many model calls one agent backend answers at once (each is a separate agent process). Read from the process environment only, not from `.env`. See [Agent backends](./agents.md). | `4` |
| `POLARIS_LLM_FAKE_FALLBACK` | Fall back to the built-in fake LLM provider when no route is configured (key-less demos and tests only). Strictly opt-in: off unless explicitly set to `1`, and never set by the product itself. When off, AI features return `LLM_NOT_CONFIGURED` instead of fabricated content. | (unset) |
| `POLARIS_CHAT_AGENT_ENABLED` | PolarisBuddy's multi-turn tool loop. Each round re-sends the conversation history and tool definitions, so it uses more than one-shot chat. | on; set `0` to turn off |
| `POLARIS_GITHUB_REPO` | Upstream `owner/name` repository. Currently unread by the backend (feedback opens a pre-filled GitHub new-issue page straight from the frontend). | `ZJU-REAL/Polaris` |
| `POLARIS_PUBLIC_BASE_URL` | Engine root used to build stdio MCP download links. HTTP MCP always reuses the origin of its current `/mcp` request and ignores this setting. | (empty), for example, `http://127.0.0.1:8000` |
| `POLARIS_MCP_DOWNLOAD_LINK_TTL_SECONDS` | Lifetime of signed paper-figure download links, in seconds. Values are limited to 60 seconds through 24 hours. | `900` |
| `POLARIS_DATABASE_URL` | SQLite database URL (SQLite is the only supported database). The desktop app always sets it to `polaris.db` in its user-data folder; when running from source it defaults to a file in the working directory. | `sqlite+aiosqlite:///./polaris_dev.db` |
| `POLARIS_OPENAI_COMPAT_BASE_URL` | Fallback base URL for OpenAI-compatible model routes that leave `base_url` empty. Agent backends, providers, and API keys themselves are configured in-app (Settings → Models & agents) and stored in the database. | `https://api.deepseek.com/v1` |
| `POLARIS_S2_API_KEY` | Semantic Scholar API key. Optional; without it rate limits are stricter. | (empty) |
| `POLARIS_OPENALEX_MAILTO` | Contact email for the OpenAlex polite pool. | `polaris@example.org` |
| `POLARIS_DATA_DIR` | Folder for PDFs, exports, experiment logs, and the file projection. The desktop app sets it to `engine/data/` in its user-data folder unless you set it yourself; relative paths are resolved once at startup. | `./data` |
| `POLARIS_OUTBOUND_PROXY` | HTTP proxy for outbound literature API calls (arXiv, Semantic Scholar, OpenAlex) when direct access is unreliable. Not used for model or internal traffic. | (empty), e.g. `http://127.0.0.1:7897` |
| `POLARIS_PIP_INDEX_URL` | Optional pip mirror used on the remote experiment servers. | (empty), e.g. `https://pypi.tuna.tsinghua.edu.cn/simple` |

Speech settings aren't environment variables. Configure the external endpoint,
model, voice, speed, and per-segment limit under **Manage > LLM admin > Speech
model**. Polaris stores the configuration in the database.

> [!NOTE]
> Agent backends, model providers, their keys, and the model routing table are all managed in the
> app under **Settings → Models & agents**, not through environment variables.

## Engine image build arguments (dev and tests)

Only for building the engine image used by the desktop smoke/E2E tests and golden recording
(`make engine-image`, see [Development](development.md)). Pass them on the `make` command line.

| Variable | Purpose | Default / example |
| --- | --- | --- |
| `GITHUB_PROXY` | Prefix to accelerate the TeX base image's GitHub downloads (tectonic binary, CJK font pack) on networks that cannot reach GitHub directly. | (empty), e.g. `https://gh-proxy.com/` |
| `APT_MIRROR` | Debian mirror hostname for the TeX base image's apt installs. | (empty), e.g. `repo.huaweicloud.com` |
| `PIP_INDEX_URL` | Alternate PyPI mirror for the engine image build. | (empty), e.g. `https://pypi.tuna.tsinghua.edu.cn/simple` |

## MCP stdio variable

When running the external MCP server over stdio for a local desktop client
(`python -m app.mcp`), the caller is identified by an environment variable
rather than a JWT. Set `POLARIS_PUBLIC_BASE_URL` in the application settings
table when you need figure tools to return absolute download URLs.

| Variable | Purpose | Example |
| --- | --- | --- |
| `POLARIS_MCP_USER_EMAIL` | Optional. Email of the user the stdio MCP process acts as. Unset, it acts as the local user (`local@polaris.desktop`). | `local@polaris.desktop` |

## Model routing

Polaris routes each research stage to an agent backend or a specific provider and model through a
DB-backed routing table, editable under Settings → Models & agents. This lets cheap models handle
scoring while strong models handle idea debate and paper drafting. All calls go through the single
`app/core/llm/` abstraction; see [Architecture](architecture.md#the-llm-abstraction-and-model-routing).

### Provider protocols

Each provider speaks one protocol, chosen when you add it:

| Protocol | Endpoint | Use it for |
|---|---|---|
| OpenAI Chat Completions (`openai_compat`) | `POST {base_url}/chat/completions` | DeepSeek, Qwen, vLLM, LiteLLM and most gateways |
| OpenAI Responses (`openai_responses`) | `POST {base_url}/responses` | OpenAI models and gateways that only expose the Responses API |
| Anthropic Messages (`anthropic`) | `POST {base_url}/v1/messages` | Claude via the native API |

The Responses provider supports text, images, tool calls, reasoning effort (`reasoning.effort`) and
streaming. Embedding and rerank calls on a Responses provider use the same endpoints as the Chat
Completions provider (`/embeddings`, `/rerank`).

### Reasoning effort

Each route can also carry a **reasoning effort** — how much the model is allowed to think before
answering. Leave it unset (the default) and Polaris sends no effort parameter at all, so the model
uses its own default; existing routes are unaffected. Levels are `none`, `minimal`, `low`, `medium`,
`high`, `xhigh`, `max`, sent as `reasoning_effort` to OpenAI-compatible endpoints, as
`reasoning.effort` to the Responses API, and as `output_config.effort` to the native Anthropic API.

Support varies by model, and not every level is valid on every model that accepts the parameter —
a model may accept `low` but reject `minimal`. Polaris does not keep a per-model whitelist. If the
server rejects the request because of the effort parameter, the call is retried once without it and
a warning is logged, so an effort set on a model that cannot use it degrades to the model's default
instead of breaking the stage. Embedding and rerank routes have no effort setting.

Lower effort on high-volume mechanical stages (relevance scoring, extraction) cuts both cost and
latency; raise it on stages where correctness matters more than spend (self-verification, review).

### Context window and input budgets

A route can record its model's **context window** (in tokens), and a few stages expose **input
budgets**: how many characters of material the stage puts into one call. The two are separate on
purpose. The window is what the model can take; the budget is what the stage chooses to send, a
trade-off between thoroughness and cost. A model with a 1M-token window does not have to receive
1M tokens on every paper.

| Stage | Budget | Default | Range (characters) |
|---|---|---|---|
| Librarian (`librarian`) | Paper full text sent to the wiki compile | 24,000 | 4,000 – 2,000,000 |
| Idea Forge (`forge`) | Total knowledge-base context | 12,000 | 2,000 – 2,000,000 |
| Idea Forge (`forge`) | Wiki excerpt per paper | 800 | 200 – 50,000 |

The defaults are the values that were hard-coded before budgets became configurable, so leaving the
fields empty changes nothing. The number of papers Idea Forge reads is still the `max_context_papers`
knob on the task.

Rules:

- A budget belongs to the stage's own route. A stage that follows *Default* uses the default budget;
  set a model for the stage to change it.
- When the route has a context window, a budget may be at most `window × 2` characters (about half
  the window at four characters per token, leaving room for the system prompt and the answer).
  Larger values are refused on save, and a default larger than that is lowered to fit.
- Material over the budget is cut from the end: the full text keeps its beginning, and the forge
  context cuts each excerpt first, then the joined context, so the least relevant papers drop out.

The routing table shows the budget in effect next to each field. Budgets are registered in
`app/core/llm/budgets.py`; adding one there makes it appear in the settings page and in save
validation.
