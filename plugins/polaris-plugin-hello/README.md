# polaris-plugin-hello

The official Polaris seed plugin. It does one visible thing — expose a
`hello-panel` service with a configurable greeting — and exists so that:

- the marketplace always has at least one installable, end-to-end-tested entry;
- third-party developers have a copyable template that already passes every
  rule the install engine enforces.

To write your own plugin, copy this directory and work through the sections
below. The full loading model is documented in
[`docs/plugins.md`](../../docs/plugins.md).

## Anatomy

```
polaris-plugin-hello/
├── package.json   # npm metadata + the `polaris` manifest (see table below)
├── src/index.ts   # plugin source: Config schema + apply(ctx, config)
└── dist/index.js  # single-file bundle — the ONLY thing the kernel loads
```

A plugin is a [cordis](https://github.com/cordiverse/cordis) plugin: an object
with `name`, an optional schemastery `Config`, and `apply(ctx, config)`. Inside
`apply` you have three basic moves, all demonstrated in `src/index.ts`:

| move | call | what it is for |
| --- | --- | --- |
| validate config | `export const Config = Schema.object({...})` | the settings UI renders and validates config through this schema |
| own a side effect | `ctx.effect(() => { ...; return cleanup })` | anything you start must be returned as a cleanup; disable/uninstall disposes it |
| expose a service | `ctx.provide('hello-panel', service)` | other plugins consume it via `ctx.get(...)`; unmounted with your plugin |

## The `polaris` manifest

Lives in `package.json` under the `polaris` key. Validated on install; an
invalid manifest fails the install and leaves nothing on disk.

| field | required | meaning |
| --- | --- | --- |
| `kind` | yes | one of `datasource` \| `record-kind` \| `runner` \| `agent-tool` \| `workflow` \| `discipline` \| `panel` |
| `entry` | yes | package-relative path to the **single-file bundle** (here `dist/index.js`) |
| `description` | no | string, or `{ zh, en }` record; linted on install (no imperative/injection text, no invisible unicode) |
| `permissions` | no | declared summary (`network` / `filesystem`); **shown to users as-is, not enforced in v1** — declare honestly |
| `runtime` | no | `in-process` (the only runtime actually loaded in v1) \| `python-edge` \| `subprocess-mcp` \| `container` |
| `locales` | no | locales your strings cover, e.g. `["zh", "en"]` |

## Hard rules the install engine enforces

1. **Single-file bundle.** `entry` must point at one self-contained JS file
   (Obsidian model). The kernel imports it directly — there is no `npm install`
   step and no `node_modules` at runtime, so bundle every runtime dependency
   (this template bundles schemastery via esbuild; that is why all deps are
   `devDependencies`). Build with:

   ```sh
   pnpm run build   # esbuild src/index.ts --bundle → dist/index.js (CJS)
   ```

2. **Install and enable are separate.** Installing writes files and registers a
   *disabled* config-tree entry; not one line of your code runs until the user
   enables it. Never rely on install-time side effects.

3. **Trust is pinned to content hashes, not names.** At install time the engine
   records the tarball's sha512 and the entry file's sha256. The entry hash is
   re-verified before every load — if the file on disk changes, the plugin is
   forcibly disabled with a warning. Ship a new version instead of patching
   files in place.

4. **Descriptions are linted.** `description` (both the npm one and the
   manifest one) must not contain instruction-like text ("ignore previous…",
   "you must…") or invisible unicode. This guards agent-facing metadata against
   prompt injection; plain factual descriptions always pass.

## Publishing

npm handles distribution: `npm publish` the package, then get it listed in the
official index (`market/index.json` in the main repo — see the policy in
`market/README.md`; until the marketplace opens, listing is by Polaris team
review only). Clients install the exact listed version from the npm registry
and verify it against `dist.integrity`.
