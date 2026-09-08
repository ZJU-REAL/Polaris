# Plugins (desktop)

The desktop app has a plugin kernel (`@polaris/kernel`, a [cordis](https://github.com/cordiverse/cordis)
runtime hosted in the Electron main process). Plugins extend the shell without forking it: a
plugin is an npm package that the app downloads, verifies, and mounts into its config tree.
This page documents the loading model, the marketplace, and how to write a plugin. The seed
plugin [`plugins/polaris-plugin-hello`](https://github.com/ZJU-REAL/Polaris/tree/main/plugins/polaris-plugin-hello)
is the living template — everything below is demonstrated there and exercised end-to-end in CI.

## The loading model: the config tree is the source of truth

The kernel does not load "whatever is on disk". A persistent **config tree** (stored in the
app's local SQLite database) is the single source of truth for what runs: each entry names a
plugin (a built-in `cordis:*` specifier or a `file://` bundle), carries its config, and has a
`disabled` flag. On startup the loader mounts exactly this tree; enabling, disabling,
configuring, and removing a plugin are all edits to the tree. The tree survives restarts, can
be exported and imported whole, and a last-good snapshot guards imports.

Consequences worth internalizing:

- deleting files on disk does not "uninstall" a plugin — the tree entry is authoritative, and
  a missing or altered file makes the entry fail verification (below) rather than vanish;
- state you see in the plugins list is runtime fact (`active` / `disabled` / `error` /
  `pending`), while the `disabled` flag is persistent user intent.

## Install and enable are separate

Installing a plugin **never runs its code**. The install pipeline downloads the exact listed
version from the npm registry, verifies the tarball against its `sha512` integrity, extracts
it, validates the `polaris` manifest, and registers a **disabled** entry in the config tree.
Only when the user explicitly enables the entry is the bundle imported and applied. Upgrades
follow the same rule: a newly installed version is forced back to disabled, so "update"
can never mean "silently execute new code".

Uninstalling is refused while the entry is enabled — disable first, then uninstall removes
the tree entry, the installed files, and the install record.

## Content-hash pinning

Trust binds to content, not names. At install time the kernel records:

- the tarball's `sha512` (the same SRI value npm publishes), and
- the entry file's `sha256`.

The entry hash is re-verified at two points: a **startup scan** before the tree mounts
(a tampered plugin is forcibly disabled with a note, without disturbing other plugins), and
an **enable-time guard** covering files modified mid-session. A hash mismatch means the
plugin will not load until it is reinstalled. Patching an installed bundle in place is
therefore never a supported workflow — publish a new version.

## The market: index and endpoint

What is *listed* lives in one JSON file — [`market/index.json`](https://github.com/ZJU-REAL/Polaris/blob/main/market/index.json)
in the main repository, documented in [`market/README.md`](https://github.com/ZJU-REAL/Polaris/blob/main/market/README.md).
The app fetches it from a configurable **endpoint** (default: the file's raw GitHub URL), so
mirrors and self-hosted indexes are first-class; an empty value in settings resets to the
official source. Distribution itself is plain npm: the index says *what* is listed, the npm
registry serves the bytes, and the client verifies them independently.

Each entry carries a kind, publisher, declared permissions, a quality tier
(bronze/silver/gold/platinum), and governance badges such as `official`.

## Writing a plugin from the template

1. **Copy the template.** Start from
   [`plugins/polaris-plugin-hello`](https://github.com/ZJU-REAL/Polaris/tree/main/plugins/polaris-plugin-hello);
   its README documents every manifest field. The plugin itself is a cordis plugin: an object
   with `name`, an optional schemastery `Config`, and `apply(ctx, config)` that registers
   recyclable effects (`ctx.effect`) and services (`ctx.provide`).
2. **Fill in the `polaris` manifest** in `package.json`: `kind` (one of the seven extension
   points), `entry` (path to the bundle), honest `permissions`, and descriptions.
3. **Bundle to a single file.** `entry` must point at one self-contained JS file; there is no
   `npm install` at runtime, so bundle every dependency (the template uses esbuild →
   `dist/index.js`). Installs with a multi-file entry are rejected.
4. **Publish to npm and get listed.** Clients install the exact version named by the index
   entry and verify integrity end-to-end.

## v1 boundaries, stated plainly

- **Permissions are display-only.** The `permissions` declaration is shown to users as-is and
  linted, but not enforced by a sandbox yet. Treat it as an honesty contract.
- **Official source only.** Until the contract freezes, `market/index.json` accepts entries
  reviewed and published by the Polaris team; third-party listing PRs are closed.
- **Single-file bundle is a hard rule.** The kernel resolves nothing at load time.
- **In-process runtime only.** The manifest admits `python-edge` / `subprocess-mcp` /
  `container` runtimes for forward compatibility, but v1 only loads `in-process` bundles.
- **Descriptions are linted** against instruction-like text and invisible unicode (metadata
  is agent-visible; prompt-injection via descriptions is a real attack class).
- **The `panel` kind has no rendering surface yet.** An installed `panel` plugin does not show
  up anywhere in the UI; the services it registers are only consumable by other plugins. A real
  panel surface is future work.
