# Configuration

Most of Polaris is configured inside the app (Settings) and stored in its database. A small set of
values are environment variables read by the engine at startup. You rarely need them: the desktop
app starts its engine with working defaults and sets the database path and data folder itself.

Where the engine gets its environment:

- **Desktop app.** The engine inherits the app's environment. To set a variable, start the app from
  a terminal with that variable set (for example `POLARIS_OUTBOUND_PROXY=http://127.0.0.1:7897`
  before the app's command). The Claude variables are the exception: on macOS and Linux the app
  also reads them from your login shell at startup, so an `export` in `~/.zshrc` or `~/.bashrc` is
  enough — see [Model](#model).
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
| `POLARIS_ENCRYPTION_KEY` | Fernet key that encrypts stored secrets (SSH and connection credentials, MCP server environment variables, literature and MinerU keys; the Claude API key is not stored — it is read from the environment). The desktop app generates one per install. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Empty derives a key from the secret key (not allowed with `POLARIS_ENV=prod`). Do not set a placeholder — it is not a valid Fernet key. Values encrypted with the key derived from the old default secret key are still readable and are re-encrypted with this key at startup; values encrypted with any other earlier key are not. | (empty) |
| `POLARIS_CORS_ORIGINS` | Extra origins (comma-separated, full origins) allowed to call the engine from a browser page. `app://polaris` is always allowed; `*` is never used. The Vite dev server proxies `/api` same-origin and does not need this. Set it when a page on another origin talks to the engine directly, for example `http://localhost:6274` for the MCP Inspector, or `http://localhost:5173` if you point the frontend at the engine without the proxy. Origins listed here may also obtain a local session when `POLARIS_LOCAL_SESSION_SECRET` is unset. | (empty) |
| `POLARIS_ALLOWED_HOSTS` | Extra `Host` header names (comma-separated, any port) the engine accepts. `127.0.0.1`, `localhost`, and `[::1]` are always accepted; any other host gets `400`, which blocks DNS rebinding. `*` turns the check off — only for debugging setups that know why they need it. | (empty) |
| `POLARIS_LOCAL_SESSION_SECRET` | Set by the desktop app on every launch. When set, `POST /api/auth/local-session` only answers requests carrying the same value in `X-Polaris-Session-Secret`. When unset (running from source, tests), it answers requests with no `Origin` header or an allowed one. Do not set it by hand. | (set by the desktop app) |
| `POLARIS_INSTANCE_ID` | Set by the desktop app on every launch and echoed by `/api/health`, so the app can tell its own engine apart from another process on the same port. | (set by the desktop app) |
| `POLARIS_SESSION_LIFETIME_SECONDS` | Session lifetime in seconds. There is no refresh-token mechanism, so the default is long. | `2592000` (30 days) |
| `POLARIS_LLM_FAKE_FALLBACK` | Fall back to the built-in fake LLM provider when there is no Claude API key (key-less demos and tests only). Strictly opt-in: off unless explicitly set to `1`, and never set by the product itself. When off, AI features return `LLM_NOT_CONFIGURED` instead of fabricated content. | (unset) |
| `POLARIS_CHAT_AGENT_ENABLED` | PolarisBuddy's multi-turn tool loop. Each round re-sends the conversation history and tool definitions, so it uses more than one-shot chat. | on; set `0` to turn off |
| `POLARIS_GITHUB_REPO` | Upstream `owner/name` repository. Currently unread by the backend (feedback opens a pre-filled GitHub new-issue page straight from the frontend). | `ZJU-REAL/Polaris` |
| `POLARIS_PUBLIC_BASE_URL` | Engine root used to build stdio MCP download links. HTTP MCP always reuses the origin of its current `/mcp` request and ignores this setting. | (empty), for example, `http://127.0.0.1:8000` |
| `POLARIS_MCP_DOWNLOAD_LINK_TTL_SECONDS` | Lifetime of signed paper-figure download links, in seconds. Values are limited to 60 seconds through 24 hours. | `900` |
| `POLARIS_DATABASE_URL` | SQLite database URL (SQLite is the only supported database). The desktop app always sets it to `polaris.db` in its user-data folder; when running from source it defaults to a file in the working directory. | `sqlite+aiosqlite:///./polaris_dev.db` |
| `POLARIS_S2_API_KEY` | Semantic Scholar API key. Optional; without it rate limits are stricter. | (empty) |
| `POLARIS_OPENALEX_MAILTO` | Contact email for the OpenAlex polite pool. | `polaris@example.org` |
| `POLARIS_DATA_DIR` | Folder for PDFs, exports, experiment logs, and the file projection. The desktop app sets it to `engine/data/` in its user-data folder unless you set it yourself; relative paths are resolved once at startup. | `./data` |
| `POLARIS_OUTBOUND_PROXY` | HTTP proxy for outbound literature API calls (arXiv, Semantic Scholar, OpenAlex) when direct access is unreliable. Not used for model or internal traffic. | (empty), e.g. `http://127.0.0.1:7897` |
| `POLARIS_PIP_INDEX_URL` | Optional pip mirror used on the remote experiment servers. | (empty), e.g. `https://pypi.tuna.tsinghua.edu.cn/simple` |

Speech settings aren't environment variables. Configure the external endpoint,
model, voice, speed, and per-segment limit under **Settings → Speech**. Polaris
stores the configuration in the database.

> [!NOTE]
> The model is not configured in the app. Polaris uses the Claude API with one fixed model, and the
> key and endpoint come from the environment variables in the next section. **Settings → Model**
> only shows what is in effect.

## Claude API variables

These are read by the engine at startup; a change needs a restart of Polaris. They are also valid
in `src/backend/.env` when running from source. See [Model](#model) for how they are used.

| Variable | Purpose | Default / example |
| --- | --- | --- |
| `POLARIS_CLAUDE_API_KEY` | Claude API key, first in the resolution order. | (empty), e.g. `sk-ant-...` |
| `POLARIS_CLAUDE_BASE_URL` | Endpoint for Claude calls. When set, it always overrides the endpoint, whichever key is used. | (empty), e.g. `http://your-gateway:4000` |
| `ANTHROPIC_API_KEY` | Claude API key, second in the resolution order. | (empty) |
| `ANTHROPIC_BASE_URL` | Endpoint used when `ANTHROPIC_API_KEY` is the key in use. | (empty → `api.anthropic.com`) |
| `LITELLM_API_KEY` | Key for a LiteLLM gateway that serves Anthropic's native `/v1/messages`; third in the resolution order. | (empty) |
| `LITELLM_API_BASE` | The gateway's address, used when `LITELLM_API_KEY` is the key in use. | (empty), e.g. `http://your-gateway:4000` |
| `POLARIS_CLAUDE_MODEL` | The model every call uses. Must be an adaptive-thinking Claude model (Opus 5 / Sonnet 5 / Fable 5 family). There is no UI for it. | `claude-opus-5` |
| `POLARIS_EMBEDDING_MODEL` | Embedding model requested from the gateway (`{base}/v1/embeddings`). | `BGE-M3` |
| `POLARIS_RERANK_MODEL` | Rerank model requested from the gateway (`{base}/v1/rerank`). | `BGE-Reranker-V2-M3` |
| `POLARIS_CLAUDE_REFUSAL_FALLBACK` | Server-side fallback (beta): when Claude's safety classifiers decline a request, it is retried on another Claude model. Set `0` if a proxy rejects the parameter. | on |

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

## Model

Polaris uses the **Claude API only**, with **one fixed model**: `claude-opus-5`. There is no
provider list, no per-stage model table, and no model picker in the app. To use a different Claude
model, set `POLARIS_CLAUDE_MODEL` (an adaptive-thinking model: Opus 5 / Sonnet 5 / Fable 5 family).

### Key and endpoint

The engine reads the key and endpoint from environment variables at startup. The first source that
has a key wins, and the endpoint comes from that same source:

1. `POLARIS_CLAUDE_API_KEY` / `POLARIS_CLAUDE_BASE_URL`
2. `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`
3. `LITELLM_API_KEY` / `LITELLM_API_BASE` — a LiteLLM gateway that serves Anthropic's native
   `/v1/messages`

If the winning source has no base URL, requests go to `api.anthropic.com`. `POLARIS_CLAUDE_BASE_URL`,
when set, always overrides the endpoint. A base URL may be written with or without a trailing `/v1`.

The key follows its own endpoint on purpose: a computer often has both an official
`ANTHROPIC_API_KEY` and a gateway's `LITELLM_*` pair, and mixing them would send one side's key to
the other.

### Setting the variables for the desktop app

Apps started from Finder, the Dock, or a Linux launcher do not inherit what your shell exports. So
on macOS and Linux Polaris reads these variables once at startup from your login shell
(`$SHELL -ilc env`, with a 4-second timeout). It takes only the names listed above, and never logs
their values. In practice:

```bash
# ~/.zshrc or ~/.bashrc
export ANTHROPIC_API_KEY=sk-ant-...
# or, for a LiteLLM gateway:
export LITELLM_API_KEY=sk-...
export LITELLM_API_BASE=http://your-gateway:4000
```

Then restart Polaris. On Windows, set them as user environment variables. A change always needs a
restart of Polaris. See [Desktop](desktop.md#claude-variables-from-the-login-shell) for the
mechanics.

Without a key, AI features return `LLM_NOT_CONFIGURED` (HTTP 503) and the app tells you to set the
environment variable and reopen Polaris. `POLARIS_LLM_FAKE_FALLBACK=1` swaps in a fake provider when
there is no key; it exists for tests and key-less demos only.

### Embeddings and rerank

Claude has no embedding or rerank model, so these come from the gateway's OpenAI-compatible
endpoints, `{base}/v1/embeddings` and `{base}/v1/rerank`, with the models `BGE-M3` and
`BGE-Reranker-V2-M3` (override with `POLARIS_EMBEDDING_MODEL` and `POLARIS_RERANK_MODEL`). They are
available only when a gateway base URL is set.

With a direct Anthropic key there are no embeddings and no rerank: semantic search falls back to
keyword search and results are not re-ranked. Features degrade; nothing crashes. See
[Embedding & Retrieval](embedding-and-retrieval.md).

### Settings → Model

**Settings → Model** (`/settings?tab=model`) is read-only. It shows "Claude · claude-opus-5", the
host requests go to, a **Check connection** button (it makes one tiny live call), and the state of
the search-index and rerank models. The old links `/settings?tab=llm` and `/settings?tab=agents`
redirect there. Usage and the call log are under **Settings → Usage**.

### What differs per research stage

Every stage uses the same model. Two things still differ per stage; both are internal and not
configurable:

- **Patience**: timeouts and retries.
- **Thinking depth** (effort): low for short scoring and extraction calls, medium for the assistant
  and structured generation, high for long writing and compiling.

Input budgets — how much material a stage puts into one call — are derived from the model's context
window (1M tokens on `claude-opus-5`). For example, the wiki compiler reads up to about 160,000
characters of a paper's body. Material over a budget is cut from the end.

### How calls are made

All calls go through the single `app/core/llm/` layer (see
[Architecture](architecture.md#the-llm-layer)) and use the official `anthropic` Python SDK.
Thinking is adaptive, sampling parameters such as temperature are not sent, and long outputs are
streamed. Prompt caching is used for stable system prompts and for the assistant's tool loop. If
Claude's safety classifiers decline a request, the server-side fallback (beta) retries it on another
Claude model; set `POLARIS_CLAUDE_REFUSAL_FALLBACK=0` if a proxy rejects that parameter.

### Experiments

When an experiment sets `eval_model`, the experiment machine gets an `llm_config.json` with the
same key and an OpenAI-compatible base URL: `{gateway}/v1`, or `https://api.anthropic.com/v1` with a
direct key. See the [Experiments guide](experiments.md).
