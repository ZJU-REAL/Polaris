# Polaris plugin market index

This directory holds the official Polaris plugin market index. Desktop clients
fetch `index.json` over its raw URL (the endpoint is configurable, so mirrors
and self-hosted indexes work the same way), validate it against the schema
below, and render the market view from it. npm handles distribution and
versioning; this index only decides **what is listed**.

## Index file shape

```jsonc
{
  "schemaVersion": 1,   // bump on breaking shape changes; clients reject unknown majors
  "plugins": [ /* MarketIndexEntry[] */ ]
}
```

## Entry schema (`MarketIndexEntry`)

The canonical TypeScript contract lives in `src/kernel/src/market/contract.ts`.

| field | type | notes |
| --- | --- | --- |
| `name` | string | npm package name (`polaris-plugin-*` or `@scope/polaris-plugin-*`) |
| `version` | string | the listed version; installs resolve exactly this version from the npm registry |
| `kind` | enum | one of the seven extension points: `datasource` \| `record-kind` \| `runner` \| `agent-tool` \| `workflow` \| `discipline` \| `panel` |
| `description` | string | short human description; linted on listing (no imperative/injection text, no hidden unicode) |
| `publisher` | string | who published it (npm account or org) |
| `permissions` | object | declared summary: `{ "network"?: boolean, "filesystem"?: boolean }` — shown to users as-is, not enforced in v1 |
| `tier` | enum | quality tier: `bronze` \| `silver` \| `gold` \| `platinum` (see below) |
| `badges` | string[] | governance badges, e.g. `official`, `verified`, `preview`, `insecure` |

### Quality tiers

Machine-checkable ladder (modeled on Home Assistant's scale):

- **bronze** — valid manifest + config schema + tests exist
- **silver** — incremental sync / idempotency (datasources) or dry-run support (runners)
- **gold** — documentation + bilingual (zh/en) strings
- **platinum** — in production use

## Official-source rule

Until the marketplace opens to third parties, **only entries reviewed and
published by the Polaris team are accepted here**. Pull requests adding
third-party entries will be closed. When the contract is frozen and the market
opens, this file's history is the audit trail of what was listed when.

Listing is index-side; installation is client-side: the install engine pulls
metadata from the npm registry, verifies the tarball against its `sha512`
integrity, extracts it, validates the `polaris` manifest (the entry must be a
single-file bundle), and records content hashes that are re-verified before
every load.
