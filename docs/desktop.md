# Desktop client (Electron shell)

The desktop app is Polaris's primary form: an **offline, single-machine build**. The packaged
app ships its own Python backend and boots it locally on first launch — no Docker, no Postgres,
no Redis, no account, no server address. Sign-in does not exist in this form: the backend runs
with `POLARIS_PROFILE=desktop` and the frontend silently adopts a local session (the machine's
owner is their own admin).

Connecting to a remote multi-user server is still supported: whenever no local engine is
available (or the bootstrap fails), the renderer falls back to the classic
"shell plus a remote server" flow, with all heavy state on the server.

The code lives in `src/desktop/`, a sibling of `src/frontend` and `src/backend` with its own
`package.json` (one pnpm workspace at the repo root).

## Process layers

```
Renderer (the existing React code in src/frontend, sandbox: true)
  ↓ one channel: ipcRenderer.invoke('polaris:rpc') / .on('polaris:event')
Main (shell and arbitration: window, menu, protocol, config)
  └─ @polaris/kernel — a cordis plugin runtime mounted in the main process
       ├─ storage        SQLite persistence (userData/kernel/storage.db);
       │                 holds the plugin config tree
       ├─ Loader + SqliteTree   mounts plugins from the persisted config tree
       └─ legacy-engine  spawns the local Python backend and health-checks it
            → http://127.0.0.1:18080 — the full FastAPI backend, run locally:
              uv-bootstrapped venv, SQLite database, in-process task queue,
              POLARIS_PROFILE=desktop (no external services, no login)

Remote server (api / worker / postgres / redis) — the fallback path, reached
directly by the renderer when no local engine is up (or one is configured away)
```

**The renderer talks to the backend over HTTP in both forms; main never proxies the API.** The
local engine is a real HTTP server on loopback, so `lib/api.ts`, `lib/sse.ts` and `lib/ws.ts`
work against it unchanged. The moment main starts proxying, SSE streaming, WebSocket upgrades,
blob streams and token handling all have to be reimplemented there. The shell exists to *add*
capabilities, not to take over the network.

The kernel is also the plugin platform: what runs is driven by a persistent **config tree**
(seeded with `desktop-probe` / `sources` / `legacy-engine` on first launch, user edits are
authoritative afterwards). See [Plugins](plugins.md) for the loading model and the market.

## The local engine: configuration and precedence

The `legacy-engine` entry is seeded **disabled** in the config tree; the engine's parameters are
computed fresh on every launch and injected in memory only — the tree stores the user's intent
(whether the plugin should exist), never a stale engine spec. Configuration sources, in order:

1. **`POLARIS_DESKTOP_ENGINE`** — explicit, for development and debugging. Two forms:
   - `docker:<image>:<backendDirAbs>` — run the backend image with the source dir mounted,
     e.g. `docker:polaris-api-test:local:/repo/src/backend`;
   - `command:<json argv>` — spawn an arbitrary command, e.g. `command:["python","-m","uvicorn",...]`.

   `POLARIS_DESKTOP_ENGINE_CONTAINER` and `POLARIS_DESKTOP_ENGINE_PORT` override the container
   name and host port for parallel shell instances (E2E runs on one machine); the packaged
   bootstrap does not read them.
2. **Packaged auto-bootstrap** — when the app is packaged and no env is set, the installer's own
   resources bootstrap a local environment (next section). The user's machine needs neither
   Python nor Docker.
3. **Neither** (development, no env) — the entry stays disabled and the app follows the
   remote-server flow.

## First launch: bootstrap and the progress page

The installer carries two extra resources (`electron-builder` extraResources, staged by
`pnpm run stage:resources`): a pinned single-file **uv** binary and the **backend source** with a
build-time content hash. On first launch (or after an update that changed either), the app runs:

```
uv python install 3.12  →  uv venv  →  uv pip install <resources/backend>
```

entirely under `userData/engine/` (managed Python, venv, uv cache — `UV_PYTHON_PREFERENCE=only-managed`,
so a system Python is never used and never polluted). A sentinel file
(`engine/bootstrap.json` recording the uv version, backend hash, and Python version) makes every
later launch skip the whole sequence at the cost of a few file reads.

The window opens **before** the kernel starts: a first-launch waiting page polls
`kernel.engineBootstrapStatus` and shows the phase (`check` / `python` / `venv` / `install` /
`engine` / `ready`; `idle` means the embedded path was not taken, `failed` means it was and fell
back to the remote flow). The first run downloads the Python toolchain and dependencies, which
can take minutes; after that, startup cost is zero.

Every engine start runs `alembic upgrade head` under a **migration guard**: a non-empty database
is snapshotted to `engine/snapshots/<timestamp>/` first, a failed migration restores the
snapshot before re-raising, and only the last 3 snapshots are kept. A failed bootstrap or engine
start never blocks the window — it logs, reports `failed`, and the renderer falls back to the
remote-server flow.

## Where the data lives

Everything is under Electron's `userData` directory; uninstalling the app and deleting
`userData` removes every trace.

| Path | What it holds |
|---|---|
| `userData/kernel/storage.db` | The kernel's SQLite store: plugin config tree, install records |
| `userData/engine/` | Managed Python, venv, uv cache — the bootstrapped runtime |
| `userData/engine/polaris.db` | The backend's SQLite database (system of record in desktop form) |
| `userData/engine/snapshots/` | Pre-migration database snapshots (last 3) |
| `userData/engine/data/` | User files: PDFs, exports, experiment logs (`POLARIS_DATA_DIR`) |
| `userData/engine/data/workspace/` | The **file projection**: a continuously refreshed, read-only copy of your papers (`papers/`), notes (`notes/`), and library wikis (`wiki/`, an Obsidian vault). The database is the source of truth — edits here are not written back and are overwritten on the next change. |
| `userData/plugins/` | Market-installed plugin bundles |

## Why Electron rather than Tauri

We need macOS, Windows and Linux. Tauri renders through WebKitGTK on Linux, which has
known blank-window and rendering failures on NVIDIA GPUs — and Polaris's UI is exactly
the heavy-rendering combination that would suffer: pdf.js canvas, CodeMirror 6, KaTeX
and yjs. The cost (85–150MB installer, ~168MB idle RAM) is acceptable.

## Page loading: `app://polaris`

Not `file://`: Chromium treats it as an opaque origin and rejects
`new Worker('file:///...')`, which takes the pdf.js reader out entirely. The absolute
`/assets/` and `/pdfjs/cmaps/` paths would also have to change, forking the desktop
bundle from the web one.

Not a local HTTP server for the *pages* either: any process on the machine could reach that
port, which adds a network attack surface for no benefit. (The local engine does listen on
loopback, but it authenticates every request; the page origin stays `app://polaris`, and the
CSP explicitly allows connecting to the local engine's address.)

Registered as `standard + secure`, the page gets a real origin, so pushState, workers,
`localStorage`, `navigator.clipboard` and `Notification` all behave as they do over
https — and **not one line of frontend structure changes** (`vite.config.ts` keeps its
default `base`, `createBrowserRouter` stays).

`polaris://` is a separate thing: it is reserved for deep links. Do not make one scheme
serve both content and OS-level handling.

## The IPC contract

`src/desktop/src/shared/contract.ts` is the single source of truth: one method table and
one event union.

One channel rather than an `ipcMain.handle` per capability. Preload is the boundary that
ships inside the installer and is directly visible to the renderer; with per-capability
channels, every local capability added later means touching preload, main and the
renderer together. As it stands, preload is written once and adding a method touches only
the contract and the main-side implementation.

Methods are named `<domain>.<object>.<verb>`: `host.*` is shell capability, `kernel.*` is
kernel state (status, local backend address, bootstrap progress, plugin management),
`local.*` is local long-job bookkeeping (currently just `local.job.cancel`). Real local
compute does not run as a separate agent process — it runs as kernel plugins
(`legacy-engine` being the first); the stdio JSON-RPC agent of the original phase-1 design
was removed once the kernel landed (#731).

Every frontend decision about local-versus-remote reads the capability manifest
(`host.capabilities`). **Do not branch on the platform or on a version number.** A declared
capability that is unavailable at call time fails with `ERR_CAPABILITY_UNAVAILABLE` and the
frontend falls back to the server; `plugins.manage` is the first capability that is actually
`true` (it tracks whether the kernel's config tree is reachable), while `latex.compile`
still reports unavailable (the tectonic probe already runs, the implementation does not
exist yet).

## Settings that look removable and are not

| Where | Constraint |
|---|---|
| `plugins: true` in `window.ts` | The manuscript PDF preview is an `<iframe src=blob:…pdf>` rendered by Chromium's built-in pdfium, not pdf.js. Electron disables plugins by default, so dropping this makes the preview blank |
| `script-src 'wasm-unsafe-eval'` in the CSP | pdf.js ships openjpeg / qcms as WASM |
| `blob:` in `connect-src` | The annotating reader hands pdf.js a `blob:` URL and pdf.js fetches it; `'self'` does not cover `blob:` in Chromium. Drop it and only that reader breaks — the standard reader is an `<iframe>` and goes through `frame-src` |
| `style-src 'unsafe-inline'` in the CSP | CodeMirror's style-mod and KaTeX insert rules at runtime; without it the editor and formula rendering break |
| `role: 'editMenu'` in `menu.ts` | Without it, Cmd+C/V/A/Z stop working in some controls on macOS |
| Recreating the window after a server change | The preload injection and the CSP response header are both fixed at document load; `reload()` cannot refresh them |

## Frontend conventions

- Server addresses go through `src/frontend/src/lib/endpoint.ts` (`apiBase()`, `wsUrl()`,
  `portalUrl()`). Do not assemble `window.location` in components — on the web these
  functions degrade to exactly the previous relative-path behaviour.
- Desktop capabilities go through `src/frontend/src/lib/host.ts`, which is a safe no-op on
  the web. **Never read `window.polaris` in a component**, or the web build needs null
  checks everywhere.
- Share links use `portalUrl()`, not `window.location.origin`: those links are opened by
  other people in a browser, so on desktop they must point at the web portal.
- System notifications go through `lib/desktop-notify.ts`, and only for events that need a
  human or that reached a terminal state — and only while the window is unfocused.
- **Keep the auth token in `localStorage`; do not switch to Electron `safeStorage`.** This
  was tried and reverted: ad-hoc signed builds get a different signature every build, so
  the keychain ACL never matches and macOS prompts for authorisation on every launch. The
  keychain only becomes reasonable once the app has a stable Developer ID signature.
  Sessions persist through `POLARIS_SESSION_LIFETIME_SECONDS` (30 days by default), which
  does not depend on the keychain at all. (Against the local engine there is nothing to
  type in the first place: the frontend fetches the local session itself.)

## Developing and packaging

```bash
make desktop-deps          # pnpm install for the whole workspace
make desktop-dev           # build the frontend and start the shell (real app:// path)
cd src/desktop && pnpm run smoke   # loads the SPA for real; non-zero exit means failure
make desktop-dist          # stage uv + backend, build an installer (unsigned)
```

In development the shell starts with **no local engine** by default and follows the
remote-server flow; set `POLARIS_DESKTOP_ENGINE` (either form above) to exercise the local
engine chain. The packaged app boots its own engine, so it asks for nothing on first launch;
the server-address page appears only when no local engine came up. For internal server-mode
distribution, `POLARIS_DEFAULT_SERVER_URL` pre-fills the address so no internal address has to
be committed. The server can be changed later from the Server… menu item (Cmd+,).

CI covers both: `desktop-build.yml` runs the smoke test on pull requests that touch the
frontend or the shell, and `desktop-release.yml` builds all three platforms on a `v*` tag
and publishes a GitHub release. The release also ships the renderer separately as
`renderer-<version>-c<contract>.tar.gz` (built once, on Linux — the bundle is
platform-independent), so a frontend-only release can be applied in place: the client swaps
the bundle in and reloads, without reinstalling. The contract version in the filename tells
the client whether its preload is new enough to run it; see `src/desktop/src/main/updates`.

### Local packaging failures worth recognising

- **`unable to execute hdiutil … Exit code: 16`** — a dmg volume from a previous build (or
  from a manual `hdiutil attach`) is still mounted and the new run cannot unmount it, so
  you get a zip but no dmg. Clear it with
  `hdiutil detach -force "/Volumes/Polaris <version>-<arch>"` and rebuild. CI never hits
  this; its runners are clean.
- **`Application entry file "dist/main.cjs" … does not exist`** — `electron-builder` was
  invoked without building first. Use `pnpm run dist:mac`, which stages resources and builds,
  or run `pnpm run build` yourself. Note that `pnpm run smoke` builds only preload and
  smoke — not `main.cjs`.
- **The packaged app exits immediately with status 0** — that is the single-instance lock,
  not a crash. Another instance is already running.
- **`The SUID sandbox helper binary … is not configured correctly`** (Linux) — Electron
  refuses to start when `chrome-sandbox` is not owned by root with mode 4755, which is how
  npm installs it. Fix the permissions rather than passing `--no-sandbox`.

### Notes on unsigned distribution

- **macOS**: `identity: null` only means "do not sign with a Developer ID" — it does
  **not** ad-hoc sign for you. And electron-builder rewrites the bundle (icon, `app.asar`,
  `extraResources`), which invalidates the signature Electron's prebuilt binary ships
  with. The Apple Silicon kernel **refuses to execute a binary with no valid signature**,
  so users see "damaged" — and that is not a quarantine flag, so `xattr` cannot clear it.
  `build/after-pack.cjs` therefore runs `codesign --force --deep --sign -` after packaging
  and verifies the result, failing the build if it cannot. This is not a substitute for
  notarization; it only makes an unsigned build launchable. The hook skips the `*-temp`
  directories so universal builds work: `@electron/universal` requires every non-binary
  file to be byte-identical across architectures, and signing each arch separately makes
  the merge abort.
  Distribute the zip rather than the dmg (one less layer of quarantine propagation);
  first launch still needs `xattr -dr com.apple.quarantine /Applications/Polaris.app` or
  right-click → Open.
- **Windows**: prefer the portable zip, which bypasses SmartScreen's installer check.
- **Linux**: AppImage and deb. The deb maintainer comes from `author` in
  `src/desktop/package.json` and **must include an email**, or electron-builder aborts —
  a macOS-only build never exercises that path, so CI is where it surfaces.
  AppImage needs `libnss3 libgtk-3-0 libasound2` on the host. Under Ubuntu
  24.04+ AppArmor restrictions, or without a SUID `chrome-sandbox`, it needs
  `--no-sandbox` — **document that, do not disable the sandbox in code**.

## Backend side

The production CORS whitelist always includes `app://polaris` — it is a constant
(`DESKTOP_ORIGIN` in `src/backend/app/main.py`), not something to configure per deployment;
`POLARIS_CORS_ORIGINS` only adds further origins for deployments where the web frontend
lives on a different domain. The whitelist matters because every desktop request carries an
`Authorization` header, so every request triggers a preflight, and with an empty
`allow_origins` Starlette answers those preflights with 400. This cannot be worked around on
the client — injecting response headers cannot change a status code.

For server deployment topics see `docs/deployment.md`.
