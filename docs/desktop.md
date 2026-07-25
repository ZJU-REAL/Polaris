# Desktop client (Electron shell)

Same shape as the Codex app and Claude Desktop: **a shell plus a local process**, with
all heavy state (Postgres/pgvector, Redis, the ARQ worker, LLM calls) staying on the
remote server. **There is no fully offline single-machine build.**

The code lives in `src/desktop/`, a sibling of `src/frontend` and `src/backend` with its
own `package.json`.

## Why Electron rather than Tauri

We need macOS, Windows and Linux. Tauri renders through WebKitGTK on Linux, which has
known blank-window and rendering failures on NVIDIA GPUs — and Polaris's UI is exactly
the heavy-rendering combination that would suffer: pdf.js canvas, CodeMirror 6, KaTeX
and yjs. The cost (85–150MB installer, ~168MB idle RAM) is acceptable for internal use.

## Process layers

```
Renderer (the existing React code in src/frontend, sandbox: true)
  ↓ one channel: ipcRenderer.invoke('polaris:rpc') / .on('polaris:event')
Main (shell and arbitration: window, menu, protocol, config — no business logic)
  ↓ line-delimited JSON-RPC over stdio (only does real work in phase 2)
polaris-locald (child_process, Node): local compilation / directory scans / cache

Remote server (api / worker / postgres / redis) — reached directly by the renderer
```

**The renderer talks to the server directly; main never proxies the API.** The moment it
does, SSE streaming, WebSocket upgrades, blob streams and token handling all have to be
reimplemented there — which is `lib/api.ts`, `lib/sse.ts` and `lib/ws.ts` written a
second time. The shell exists to *add* capabilities, not to take over the network.

## Page loading: `app://polaris`

Not `file://`: Chromium treats it as an opaque origin and rejects
`new Worker('file:///...')`, which takes the pdf.js reader out entirely. The absolute
`/assets/` and `/pdfjs/cmaps/` paths would also have to change, forking the desktop
bundle from the web one.

Not a local HTTP server either: any process on the machine could reach that port, which
adds a network attack surface for no benefit.

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
channels, every local capability added in phase 2 means touching preload, main and the
renderer together. As it stands, preload is written once and adding a method touches only
the contract and the main-side implementation.

Methods are named `<domain>.<object>.<verb>`: `host.*` is shell capability, `local.*` is
reserved for phase-2 local compute.

The local agent uses `child_process` rather than Electron's `utilityProcess`: the latter
cannot pipe stdin, and "line-delimited JSON-RPC over stdio, swappable for a Python
process later" is the entire point of this layer (the framing matches
`src/backend/app/mcp/__main__.py`). Node reuses Electron's own runtime via
`ELECTRON_RUN_AS_NODE`, since a packaged app ships no standalone `node`.

Every frontend decision about local-versus-remote reads the capability manifest
(`host.capabilities`). **Do not branch on the platform or on a version number.** Local
implementations register through `lib/local-routes.ts`, and only `LocalUnavailable` falls
back to the server: a local business error such as a LaTeX syntax error is a legitimate
result and must not be recomputed remotely.

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
  does not depend on the keychain at all.

## Developing and packaging

```bash
make desktop-deps          # install dependencies (downloads a ~100MB Electron binary)
make desktop-dev           # build the frontend and start the shell (real app:// path)
cd src/desktop && npm run smoke   # loads the SPA for real; non-zero exit means failure
make desktop-dist          # build an installer for the current platform (unsigned)
```

On first launch the app asks for a server address and validates it against
`GET /api/health`. For internal distribution, `POLARIS_DEFAULT_SERVER_URL` pre-fills it so
no internal address has to be committed. The server can be changed later from the
Server… menu item (Cmd+,).

CI covers both: `desktop-build.yml` runs the smoke test on pull requests that touch the
frontend or the shell, and `desktop-release.yml` builds all three platforms on a `v*` tag
and publishes a GitHub release.

### Local packaging failures worth recognising

- **`unable to execute hdiutil … Exit code: 16`** — a dmg volume from a previous build (or
  from a manual `hdiutil attach`) is still mounted and the new run cannot unmount it, so
  you get a zip but no dmg. Clear it with
  `hdiutil detach -force "/Volumes/Polaris <version>-<arch>"` and rebuild. CI never hits
  this; its runners are clean.
- **`Application entry file "dist/main.cjs" … does not exist`** — `electron-builder` was
  invoked without building first. Use `npm run dist:mac`, which builds, or run
  `npm run build` yourself. Note that `npm run smoke` builds only preload, agent and
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

The production CORS whitelist must include `app://polaris` (see
`src/backend/app/main.py`). Every desktop request carries an `Authorization` header, so
every request triggers a preflight, and with `allow_origins=[]` Starlette answers those
preflights with 400. This cannot be worked around on the client — injecting response
headers cannot change a status code.

For other deployment topics see `docs/deployment.md`.
