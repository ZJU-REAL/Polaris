# Polaris for DeepSeek Harness

This package connects one Polaris account to DeepSeek Harness (DSH). It is a
DSH bundle with two independent adapters:

| Polaris capability | DSH extension point | Implementation |
| --- | --- | --- |
| MCP tools | `ctx.tools` | Official `@deepseek-ai/dsh-mcp-client` |
| Assistant skills | `ctx.skills` | Native Polaris skill provider |
| Skill attachments | `ctx.tools` | `polaris_skill_resource` |

The plugin does carry assistant skills. It does not expose them as duplicate
MCP tools. DSH receives a native skill catalog, loads full instructions through
its own `skill` tool, and fetches attachments only when needed.

## Architecture

```text
DeepSeek Harness
├── official MCP client ── POST /mcp ── Polaris tool registry
└── Polaris skill provider
    ├── GET .../v1/skills ───────────── catalog and ETag
    ├── GET .../v1/skills/{slug} ────── full skill body
    └── polaris_skill_resource ──────── one attachment
```

The backend applies the selected MCP profile to both `tools/list` and
`tools/call`. Knowing the name of a hidden tool therefore cannot bypass the
profile. The plugin applies a skill's `allowed-tools` policy to the current DSH
agent turn. It hides denied Polaris MCP schemas and also installs an execution
guard for tools discovered later in the same turn.

## Requirements

- Polaris with migration `8ff89f7fcdeb` applied.
- DeepSeek Harness `0.1.0-rc.6`.
- Node.js 20 or newer for local package builds.
- A Polaris integration token. Do not use a short-lived browser JWT in a
  persistent DSH profile.

## Prepare Polaris

Apply the database migration:

```bash
cd src/backend
.venv/bin/alembic upgrade head
```

Create a token with a current user JWT. The plaintext token is returned only
by this request; Polaris stores only its SHA-256 digest.

```bash
curl --fail-with-body \
  -X POST https://polaris.example.edu/api/integration-tokens \
  -H "Authorization: Bearer $POLARIS_SESSION_JWT" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "DeepSeek Harness",
    "scopes": ["skills:read", "mcp:read"],
    "expires_in_days": 90
  }'
```

Available scopes are:

- `skills:read`: discover and load visible assistant skills and attachments.
- `mcp:read`: connect to `/mcp` and use read-only tools.
- `mcp:write`: select `dsh-full-v1`; this also requires `mcp:read`.

The only write tool currently exposed by `dsh-full-v1` is `remember`, and it
appears only after the account turns Buddy memory on; until then it is hidden
from both discovery and invocation, matching the in-app tool surface. A
read-only Polaris account cannot use the full profile even if its token names
the write scope.

List or revoke credentials with `GET /api/integration-tokens` and
`DELETE /api/integration-tokens/{id}`. Revocation takes effect on the next
request. Demoting an account to read-only likewise blocks `/mcp` for its
existing tokens on the next request, not only for its browser session.

## Build and install the bundle

Build from this checkout, then add the directory to the DSH profile that should
receive Polaris. The DSH CLI links local bundles through its profile package
manager.

```bash
cd integrations/deepseek-harness
npm ci
npm run check
dsh plugin --profile web add "$PWD"
```

Use another profile name, such as `headless`, when appropriate. Configure the
environment before starting DSH:

```bash
export POLARIS_BASE_URL=https://polaris.example.edu
export POLARIS_DSH_TOKEN=polaris_it_replace_me
export POLARIS_DSH_TOOL_PROFILE=dsh-readonly-v1

dsh --profile web --dump-config
dsh --profile web
```

`--dump-config` should contain the `polaris-mcp` and `polaris-skills` rows. Do
not put the token directly in `cordis.patch.yml` or commit it to source control.

To remove the bundle:

```bash
dsh plugin --profile web remove @polaris-ai/deepseek-harness-plugin
```

## Bundle configuration

The bundled patch reads three environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `POLARIS_BASE_URL` | `http://127.0.0.1:8000` | Reachable Polaris server root |
| `POLARIS_DSH_TOKEN` | empty | Scoped integration token |
| `POLARIS_DSH_TOOL_PROFILE` | `dsh-readonly-v1` | Backend MCP profile |

The native plugin row also accepts these configuration fields:

| Field | Default | Purpose |
| --- | --- | --- |
| `serverName` | `polaris` | MCP namespace used for policy matching |
| `refreshIntervalMs` | `30000` | Skill catalog polling interval |
| `requestTimeoutMs` | `10000` | Polaris discovery request timeout |
| `failOnStartupError` | `false` | Fail DSH boot when discovery fails |
| `allowedToolsMode` | `enforce` | `enforce`, `advisory`, or `off` |
| `userSkillRank` | `340` | DSH candidate rank for user skills |
| `builtinSkillRank` | `360` | DSH candidate rank for built-ins |

The `serverName` value must match the official MCP client row. If a user skill
and a Polaris built-in share a slug, the user skill wins before either is sent
to DSH. `allowedToolsMode: advisory` logs violations without hiding or blocking
tools; `off` disables policy tracking entirely.

## Skill behavior

Polaris sends only names, trigger descriptions, invocation mode, policy
metadata, and attachment metadata during discovery. Full bodies are loaded on
demand through DSH's native `skill` tool.

- `invocation: auto` makes a skill model- and user-invocable.
- `invocation: manual` keeps it out of model-driven loading but allows the
  standard DSH `/skill-name` user gesture.
- `allowed-tools: null` leaves the existing tool surface unchanged.
- A non-empty `allowed-tools` list is matched against raw Polaris tool names,
  such as `search_papers`, not namespaced DSH names.
- Loading several Polaris skills in one turn intersects their allowlists. It
  never adds a tool and never restricts non-Polaris DSH tools.
- Restrictions are cleared on turn completion, turn error, or agent disposal.

Catalog responses use semantic ETags. A transient refresh error keeps the last
known catalog but marks discovery incomplete. A `401` or `403` clears cached
skills and invalidates DSH discovery, so a revoked token fails closed.

## Verification

Run the package checks locally:

```bash
npm run check
npm audit --audit-level=low
npm pack --dry-run
```

After DSH starts, verify that its tool catalog contains names such as
`mcp__polaris__search_papers`, does not contain Polaris's `skill_load`, and that
Polaris assistant skills appear in DSH's native skill catalog.

Common failures:

| Symptom | Resolution |
| --- | --- |
| Skill discovery logs HTTP 401 | Replace or recreate the expired or revoked token. |
| Skill discovery logs HTTP 403 | Add `skills:read` to a new token. Token scopes cannot be edited. |
| MCP startup logs HTTP 403 | Add `mcp:read`, or stop selecting `dsh-full-v1` without `mcp:write`. |
| No Polaris tools appear | Check `/mcp`, the profile header, and `--dump-config`. |
| A skill denies a tool | Use raw Polaris names; multiple policies intersect. |
