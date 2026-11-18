# Desktop client (Electron shell)

The desktop app is the only form Polaris ships in: an **offline, single-machine build**. The
packaged app ships its own Python backend — the engine — and boots it locally on first launch: no
Docker, no database server, no Redis, no account, no server address. Sign-in does not exist: the
frontend silently adopts a local session, and whoever uses the app is the owner. The engine is one
process with SQLite, an in-process task queue, and an in-process scheduler (see
[Architecture](architecture.md)).

> [!NOTE]
> The local engine first shipped in **v0.4.0**. Releases up to v0.3.9 are remote-only shells that
> always ask for a server address, and they cannot update themselves into the local-engine build:
> install v0.4.0 or later from [Releases](https://github.com/ZJU-REAL/Polaris/releases/latest) over
> the old app.

The first half of this page is for people using the app: installing, the first launch, network
settings, logs, where the data is, updating, uninstalling. The second half, from
[Process layers](#process-layers) on, describes how the shell is built.

## Installing

Download the build for your platform from
[Releases](https://github.com/ZJU-REAL/Polaris/releases/latest). The builds are not signed with a
paid developer certificate, so each system asks once whether to trust the app.

**macOS.** Drag Polaris into Applications and open it. macOS refuses the first time. Then:

1. Open **System Settings → Privacy & Security**.
2. Scroll down to **Security**. A line says Polaris was blocked; click **Open Anyway** and confirm
   with your password or Touch ID.
3. Open Polaris again and choose **Open** in the dialog.

On macOS 15 (Sequoia) and later this is the only way through the interface: right-clicking the app
and choosing Open no longer bypasses the check for apps that are not notarized. If you prefer
Terminal, this does the same thing in one step:

```bash
xattr -dr com.apple.quarantine /Applications/Polaris.app
```

**Windows.** SmartScreen shows "Windows protected your PC": choose **More info → Run anyway**. The
portable zip avoids the installer check altogether.

**Linux.** The AppImage needs `libnss3`, `libgtk-3-0` and `libasound2`. On Ubuntu 24.04 and later
start it with `--no-sandbox`.

## The first launch

The first launch sets up the local engine: it downloads a Python runtime and the engine's
dependencies into your data folder, about 0.9 GB on disk when it is done. The window shows the
step it is on, how much has been downloaded, and the output under **Show details**. On a good
connection this takes two to five minutes; later launches skip it and take a few seconds. Polaris
checks for 2 GB of free disk space before it starts and tells you if there is not enough.

You can quit at any point during setup. The download stops at once, and the next launch picks up
where it left off: packages that were already downloaded are not fetched again.

### Network: proxy and mirrors

The downloads come from the Python package index and from Astral's Python builds. When those
cannot be reached, Polaris switches to mirrors in mainland China by itself
(`pypi.tuna.tsinghua.edu.cn` for packages, `registry.npmmirror.com` for the Python runtime), and
remembers which source worked for the next update. Each step has a time limit and is retried, so a
stalled connection ends in a clear failure rather than an endless spinner.

The proxy is taken from the first of these that is set:

1. **Settings → About → Network → Proxy** (also offered on the page shown when setup fails), for
   example `http://127.0.0.1:7890` or `socks5://127.0.0.1:1080`.
2. The proxy variables of your shell: `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`. Polaris reads them
   from your login shell at startup, so `export` lines in `~/.zshrc` or `~/.bashrc` count even when
   the app is opened from Finder or the Dock.
3. The system proxy (macOS Network settings, Windows Internet Options).

**China mirrors** in the same place has three choices: *Automatic* (the default: use mirrors only
when the default sources fail), *Prefer* (try mirrors first) and *Never*. Changes apply the next
time setup runs, so restart Polaris after saving.

If you already use `uv` and have an index configured in `~/.config/uv/uv.toml` or in
`UV_DEFAULT_INDEX`, Polaris tries your configuration first. It only sets it aside when that
attempt fails.

An HTTP proxy set in Settings is also used by the engine when it searches literature sources
(arXiv, Semantic Scholar, OpenAlex). Model requests do not go through it.

## Logs and diagnostics

Polaris writes three log files, in the `logs` folder inside the data folder (below):

| File | What is in it |
|---|---|
| `main.log` | The app itself: startup, updates, engine start and stop |
| `bootstrap.log` | First-launch setup: every line the installer printed, which download source was used, each retry and why it failed |
| `engine.log` | The local engine's own output |

Each file is capped at 2 MB with three older generations kept beside it (`engine.log.1` …), so the
folder never grows past a few megabytes. API keys, tokens and passwords are removed before a line
is written: the value of any environment variable whose name ends in `KEY`, `TOKEN`, `SECRET` or
`PASSWORD`, credentials inside proxy addresses, and anything shaped like an API key or a bearer
token.

**Settings → About → Troubleshooting** has two buttons:

- **Open logs folder** opens that folder in Finder, Explorer or your file manager.
- **Copy diagnostics** copies a plain-text report: app and engine versions, your system, the
  engine's state, the last failure if there was one, and the last 200 lines of the engine log.
  Paste it into a bug report.

The same two buttons are on the pages shown when setup fails or the engine is not running, together
with the last lines of the actual error.

## When the engine stops

If the local engine exits or stops answering while you work, Polaris restarts it once by itself. A
notice covers the window for the few seconds this takes and goes away when the engine is back;
what you had on screen stays as it was. If the engine stops again within ten minutes, Polaris shows
the "local engine stopped unexpectedly" page with the last log lines and a **Restart Polaris**
button, rather than restarting in a loop.

## Where your data is

Everything Polaris stores is in one data folder:

| System | Data folder |
|---|---|
| macOS | `~/Library/Application Support/Polaris` |
| Windows | `%APPDATA%\Polaris` (usually `C:\Users\<you>\AppData\Roaming\Polaris`) |
| Linux | `~/.config/Polaris` |

To back up, quit Polaris and copy `engine/polaris.db`, `engine/data/` and `engine-secrets.json`.
The table under [Where the data lives](#where-the-data-lives) lists everything else.

## Updating

Polaris checks for a new version shortly after it starts and every six hours; **Settings → About**
checks on demand and shows two versions, the app's and the local engine's. There are two kinds of
update:

- **Interface only.** When a release changes nothing in the local engine, Polaris downloads the
  new interface (a few megabytes) and reloads. Nothing is reinstalled.
- **Installer.** When the release also changes the engine or the app shell, Polaris downloads the
  installer for your platform. On Windows it runs by itself. On macOS the disk image opens: quit
  Polaris, drag the new Polaris into Applications, and open it again. On Linux the downloaded
  AppImage or deb opens in your file manager: replace the old AppImage, or install the package.

After an installer update the first launch prepares the new engine's environment. The previous
environment is kept until the new one is ready, so a failed download during an update does not
leave you without a working app: Polaris starts on the previous environment and tries again at the
next launch.

## Uninstalling

Remove the app (drag it to the Trash on macOS, use Add or Remove Programs on Windows, delete the
AppImage or remove the package on Linux). Your data is not touched by this. To remove it too,
delete the data folder listed above; it holds the Python runtime and package cache (about 0.9 GB)
as well as your libraries and papers, so make a backup first if you want to keep them.

## When something goes wrong

| What you see | What to do |
|---|---|
| "The download didn’t finish" | Check the connection, or open **Network settings** on that page and set a proxy; then **Restart Polaris**. The lines on the page say which address failed. |
| "Not enough disk space" | Free at least 2 GB on the disk that holds the data folder, then restart. |
| "Setup didn’t finish" with other error lines | Restart once. If it fails the same way, use **Copy diagnostics** and report it. To start setup from scratch, quit Polaris and delete `engine/venv`, `engine/venv.next` and `engine/bootstrap.json` in the data folder; your database and papers are elsewhere and are not affected. |
| "Port 18080 is in use" | Another program is using the port Polaris needs. Quit it and restart Polaris. |
| "Another copy of Polaris is still running" | Close other Polaris windows; if none are open, restart the computer. |
| "The local engine stopped unexpectedly" | **Restart Polaris**. If it keeps happening, send the diagnostics. |

---

When no local engine is available (setup failed, or a development run without
`POLARIS_DESKTOP_ENGINE`), the app shows a page saying so, with the cause, the last error lines,
and a **Restart Polaris** button that quits and reopens the app (which retries the setup and the
engine start). There is no remote mode to fall back to.

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
       ├─ Loader + SqliteTree   mounts user-installed plugins from the config tree
       └─ legacy-engine  spawns the local Python backend, health-checks it, and
            keeps watching it (restart on exit or hang); mounted directly by the
            shell, not through the config tree
            → http://127.0.0.1:18080 — the full FastAPI backend, run locally:
              uv-bootstrapped venv, SQLite database, in-process task queue
              and scheduler (no external services, no login)
```

**The renderer talks to the engine over HTTP; main never proxies the API.** The
local engine is a real HTTP server on loopback, so `lib/api.ts`, `lib/sse.ts` and `lib/ws.ts`
work against it unchanged. The moment main starts proxying, SSE streaming, WebSocket upgrades,
blob streams and token handling all have to be reimplemented there. The shell exists to *add*
capabilities, not to take over the network.

The kernel is also the plugin platform: user-installed plugins are driven by a persistent
**config tree**. A fresh install has an empty tree. See [Plugins](plugins.md) for the loading
model and the market. **Settings → Plugins and the market are switched off for now**
(`PLUGINS_UI_ENABLED` in `src/desktop/src/main/capabilities.ts`): the market lists one demo
plugin and none of the declared plugin kinds has a host-side extension point yet, so the page
offered risk without function. The code, the `plugins.*` methods and their tests remain; set
`POLARIS_PLUGINS_UI=1` to see the tab in a development run.

## The local engine: configuration and precedence

The engine is **not** an entry in the config tree. The shell mounts the `legacy-engine` plugin
directly on the kernel (`mountEngine` in `src/kernel/src/host.ts`), with parameters computed
fresh on every launch: the venv path, the port, the per-install key file, the log file. Earlier
versions injected those parameters through a tree entry, and any later tree write (toggling any
plugin) persisted the launch command with its absolute paths; the next launch then started the
engine from that stale entry before setup had run. Trees written by those versions are cleaned
when they are loaded: entries naming a built-in this version does not have (`legacy-engine`,
`sources`, `desktop-probe`) are dropped, and a tree that still cannot be loaded is reset to empty.
Either way the previous content is saved first, to `userData/kernel/backups/` and to the
kernel's key-value store under `config-tree:backup:…`.

Configuration sources, in order:

1. **`POLARIS_DESKTOP_ENGINE`** — explicit, for development and debugging. Two forms:
   - `docker:<image>:<backendDirAbs>` — run the backend image with the source dir mounted,
     e.g. `docker:polaris-api-test:local:/repo/src/backend`;
   - `command:<json argv>` — spawn an arbitrary command, e.g. `command:["python","-m","uvicorn",...]`
     (a complete example is under [Developing and packaging](#developing-and-packaging)).

   `POLARIS_DESKTOP_ENGINE_CONTAINER` and `POLARIS_DESKTOP_ENGINE_PORT` override the container
   name and host port for parallel shell instances (E2E runs on one machine); the packaged
   bootstrap does not read them.
2. **Packaged auto-bootstrap** — when the app is packaged and no env is set, the installer's own
   resources bootstrap a local environment (next section). The user's machine needs neither
   Python nor Docker.
3. **Neither** (development, no env) — no engine starts; the app shows the "local engine is not
   running" page.

### Supervision

Once the engine has answered its first health check, the plugin keeps watching it: the child
process's exit, and a health probe every 10 seconds (three misses in a row count as a hang, and
the process is killed). Either way the engine is restarted after a one-second backoff, at most
once per ten minutes; a second failure inside that window leaves it in `failed`. The instance id
and the session secret stay the same across a restart, so the renderer's session keeps working.

The shell forwards each change to the renderer as an `engine.state` event (`restarting`,
`running`, `failed`). While the engine is restarting, `kernel.localBackend` returns no address and
`kernel.engineBootstrapStatus` reports `phase: "engine"` with `restarting: true`; the renderer
covers the page with a notice, removes it on `running`, and reloads into the failure page on
`failed`. On quit, and on any restart, the plugin's disposer stops the child (SIGTERM, then
SIGKILL after five seconds) and removes its pid record, so no engine is left behind; an engine
orphaned by a crash of the shell itself is reclaimed on the next launch by pid and instance id
(see [Securing the local engine](#securing-the-local-engine)).

## Variables from the login shell {#claude-variables-from-the-login-shell}

The engine reads its Claude API key and endpoint from environment variables (see
[Configuration](configuration.md#model)). An app started from Finder, the Dock, or a Linux desktop
launcher does not inherit what the user exports in `~/.zshrc` or `~/.bashrc`, so on macOS and Linux
the shell fills that gap once per launch, before the engine is started: it runs the user's login
shell (`$SHELL -ilc env`, 4-second timeout) and copies over only the Claude-related names —
`POLARIS_CLAUDE_API_KEY`, `POLARIS_CLAUDE_BASE_URL`, `POLARIS_CLAUDE_MODEL`,
`POLARIS_EMBEDDING_MODEL`, `POLARIS_RERANK_MODEL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`,
`LITELLM_API_KEY`, `LITELLM_API_BASE` — plus the network settings the first-launch setup and the
engine need: `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY` (and their lowercase forms),
`UV_DEFAULT_INDEX`, `UV_INDEX_URL`, `UV_PYTHON_INSTALL_MIRROR` and `POLARIS_OUTBOUND_PROXY`. A
variable the app's own environment already has wins over
the shell's. Values are never logged, and a slow or failing shell just means nothing is picked up.
On Windows this step does nothing: GUI apps already see user environment variables. Because it runs
once at startup, a changed variable needs a restart of Polaris.

The module is `src/kernel/src/shell-env.ts`, wired in `src/desktop/src/main/kernel.ts`. The
`legacy-engine` plugin then passes the variables through to the engine: `command:` mode inherits
the whole environment, and `docker:` mode forwards them by name.

## First launch: bootstrap and the progress page

The installer carries two extra resources (`electron-builder` extraResources, staged by
`pnpm run stage:resources`): a pinned single-file **uv** binary and the **backend source** with a
build-time content hash. On first launch (or after an update that changed either), the app runs:

```
uv python install 3.12  →  uv venv --relocatable venv.next  →  uv pip install <resources/backend>
   →  import check  →  swap venv.next into venv
```

entirely under `userData/engine/` (managed Python, venv, uv cache — `UV_PYTHON_PREFERENCE=only-managed`,
so a system Python is never used and never polluted). A sentinel file
(`engine/bootstrap.json` recording the uv version, backend hash, Python version and the download
source that worked) makes every later launch skip the whole sequence at the cost of a few file
reads. The logic is `src/desktop/src/main/engine-bootstrap.ts`, deliberately free of Electron so
`bootstrap-smoke` can drive it with plain Node.

**Download sources.** The two network steps (Python, dependencies) go through an ordered list of
sources, and a step that fails with one is retried with the next, two rounds in all:

| Source | Environment for `uv` |
|---|---|
| `default` | The user's own: inherited variables, proxy, and `uv.toml` all apply |
| `mirror` | `UV_NO_CONFIG=1`, `UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`, `UV_PYTHON_INSTALL_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone` |
| `official` | `UV_NO_CONFIG=1` and no index overrides; only tried when the user has a uv configuration of their own, in case that configuration is the problem |

The order is `default → mirror` (*Automatic*), `mirror → default` (*Prefer*) or `default` alone
(*Never*); the source recorded in the sentinel moves to the front in *Automatic*. Earlier versions
set `UV_NO_CONFIG=1` unconditionally, which also shut out the mirror or proxy a user had
configured for uv. The directories (`UV_PYTHON_INSTALL_DIR`, `UV_CACHE_DIR`) and
`UV_PYTHON_PREFERENCE` are given as environment variables, which take precedence over `uv.toml`,
so honouring the user's file does not loosen the isolation. `POLARIS_BOOTSTRAP_MIRROR`
(`auto`/`on`/`off`) and `POLARIS_PROXY` override the stored settings for a debugging run.

**Limits.** Python install 15 minutes, venv 3, dependency install 45; a step with no output and no
growth on disk for 10 minutes is treated as stalled. On any of these the whole process tree is
killed and the next source is tried. Free disk space is checked first (2 GB).

**Upgrades do not touch the working environment.** The new venv is built beside the old one
(`venv.next`), verified with an import check, and only then renamed into place (`venv` →
`venv.prev`, `venv.next` → `venv`, sentinel written, `venv.prev` removed). If anything fails
before the swap, `venv.next` is deleted and this launch starts the engine on the previous venv
(reported as *degraded* in the diagnostics); the sentinel is left unchanged, so the next launch
tries again. A swap interrupted halfway is repaired at the next launch.

**Cancelling.** `bootstrapEngine` takes an `AbortSignal`. Quitting during setup aborts it: the
running `uv` and its children are killed as a process group, and `before-quit` no longer waits for
setup to finish. Quitting is bounded by a 10-second deadline in any case.

The window opens **before** the kernel starts: a first-launch waiting page polls
`kernel.engineBootstrapStatus` and shows the phase (`check` / `python` / `venv` / `install` /
`engine` / `ready`; `idle` means the embedded path was not taken, `failed` means it was and did not
finish). The first run downloads the Python toolchain and dependencies, which
can take minutes; after that, startup cost is zero.

Every engine start runs `alembic upgrade head` under a **migration guard**: a non-empty database
is snapshotted to `engine/snapshots/<timestamp>/` first, a failed migration restores the
snapshot before re-raising, and only the last 3 snapshots are kept. A failed bootstrap or engine
start never blocks the window — it is written to the logs and reported as `failed` together with
a `failure` object (`code`: `disk-space`, `download`, `install`, `resources`, `engine-start` or
`engine-crashed`; plus the last output lines), and the waiting page turns into a page that names
the cause, shows those lines, and offers **Restart Polaris**, **Open logs folder** and **Copy
diagnostics**. When the engine could
not start because port 18080 is taken, the page says so instead: either an older Polaris engine is
still running, or another program holds the port (see
[Securing the local engine](#securing-the-local-engine)). The port stays 18080 because MCP
clients and the browser extension are configured with it.

## Where the data lives

Everything is under Electron's `userData` directory — the data folder listed under
[Where your data is](#where-your-data-is) (a development run from source uses a separate one,
named `polaris-desktop`). Uninstalling the app and deleting `userData` removes every trace.

| Path | What it holds |
|---|---|
| `userData/logs/` | `main.log`, `bootstrap.log`, `engine.log` and their rotated generations; see [Logs and diagnostics](#logs-and-diagnostics) |
| `userData/config.json` | Window position and the network settings (proxy, mirror mode) |
| `userData/kernel/storage.db` | The kernel's SQLite store: plugin config tree, install records |
| `userData/kernel/backups/` | Copies of the plugin config tree taken before it was cleaned or reset at startup |
| `userData/engine/` | Managed Python (`python/`), the environment (`venv/`; `venv.next/` only while an update is being prepared), uv's package cache (`uv-cache/`) and `bootstrap.json` |
| `userData/engine/polaris.db` | The engine's SQLite database (the system of record) |
| `userData/engine/snapshots/` | Pre-migration database snapshots (last 3) |
| `userData/engine/data/` | User files: PDFs, exports, experiment logs (`POLARIS_DATA_DIR`) |
| `userData/engine/data/workspace/` | The **file projection**: a continuously refreshed, read-only copy of your papers (`papers/`), notes (`notes/`), and library wikis (`wiki/`, an Obsidian vault). The database is the source of truth — edits here are not written back and are overwritten on the next change. |
| `userData/plugins/` | Market-installed plugin bundles |
| `userData/engine-secrets.json` | The engine's per-install signing and encryption keys (file mode `0600`); see [Securing the local engine](#securing-the-local-engine). Back it up together with `engine/polaris.db` — without it the saved SSH credentials and service keys in a restored database cannot be decrypted. |
| `userData/engine.pid.json` | Process id and launch id of the engine the shell started, used to stop an orphaned engine on the next launch |

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

The app does not register a `polaris://` deep-link scheme. An earlier version registered one with
an empty handler; nothing generated such links, so it was removed. If deep links are added later,
use a scheme of their own: do not make one scheme serve both content and OS-level handling.

## The IPC contract

`src/desktop/src/shared/contract.ts` is the single source of truth: one method table and
one event union.

One channel rather than an `ipcMain.handle` per capability. Preload is the boundary that
ships inside the installer and is directly visible to the renderer; with per-capability
channels, every local capability added later means touching preload, main and the
renderer together. As it stands, preload is written once and adding a method touches only
the contract and the main-side implementation.

Methods are named `<domain>.<object>.<verb>`: `host.*` is shell capability (relaunch, clipboard,
updates, `host.diagnostics`, `host.openLogsFolder`, `host.network.get` / `.set`), `kernel.*` is
kernel state (status, local backend address and run state, bootstrap progress), `plugins.*` is
plugin management and the market, and
`local.*` is local long-job bookkeeping (currently just `local.job.cancel`). The event union
carries the `job.*` progress events and `engine.state`. Real local
compute does not run as a separate agent process — it runs as kernel plugins
(`legacy-engine` being the first); the stdio JSON-RPC agent of the original phase-1 design
was removed once the kernel landed (#731).

Every frontend decision about what the shell can do reads the capability manifest
(`host.capabilities`). **Do not branch on the platform or on a version number.** A declared
capability that is unavailable at call time fails with `ERR_CAPABILITY_UNAVAILABLE` and the
frontend falls back to the engine. The only capability today is `plugins.manage`, and it reports
unavailable while `PLUGINS_UI_ENABLED` is off (when on, it tracks whether the kernel's config tree
is reachable). The `latex.compile` placeholder, which could only ever report unavailable, was
removed along with its `tectonic` probe.

`CONTRACT_VERSION` is the version of this table as the renderer sees it. It is one of the two
checks that decide whether a release can be applied as an interface-only update; see
[Developing and packaging](#developing-and-packaging).

## Settings that look removable and are not

| Where | Constraint |
|---|---|
| `plugins: true` in `window.ts` | The manuscript PDF preview is an `<iframe src=blob:…pdf>` rendered by Chromium's built-in pdfium, not pdf.js. Electron disables plugins by default, so dropping this makes the preview blank |
| `script-src 'wasm-unsafe-eval'` in the CSP | pdf.js ships openjpeg / qcms as WASM |
| `blob:` in `connect-src` | The annotating reader hands pdf.js a `blob:` URL and pdf.js fetches it; `'self'` does not cover `blob:` in Chromium. Drop it and only that reader breaks — the standard reader is an `<iframe>` and goes through `frame-src` |
| The engine origin in `img-src` and `media-src` | The structured reading view rewrites a paper's figures to `http://127.0.0.1:18080/api/structured-content-assets/…` and renders them as `<img>`. `connect-src` does not cover images, so without this the figures are blocked — in the desktop app only, since the browser dev server is same-origin |
| `style-src 'unsafe-inline'` in the CSP | CodeMirror's style-mod and KaTeX insert rules at runtime; without it the editor and formula rendering break |
| `role: 'editMenu'` in `menu.ts` | Without it, Cmd+C/V/A/Z stop working in some controls on macOS |
| The full-page reload after the first-launch waiting page | The CSP response header is fixed at document load, and the first document loads before the engine has an address; only a reload lets the page connect to it |

## Frontend conventions

- Engine addresses go through `src/frontend/src/lib/endpoint.ts` (`apiBase()`, `wsUrl()`,
  `localOrigin()`). Do not assemble `window.location` in components — in a plain browser
  (the Vite dev server) these functions degrade to same-origin relative paths.
- Desktop capabilities go through `src/frontend/src/lib/host.ts`, which is a safe no-op in
  a plain browser (plugin management, for instance, simply does not appear there).
  **Never read `window.polaris` in a component**, or every caller needs null checks.
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

In development the shell starts with **no local engine** by default; set
`POLARIS_DESKTOP_ENGINE` to exercise the local engine chain. Three ways:

**A backend from source** (`command:`). Use absolute paths, a Python environment that has the
backend installed (`make venv`), and a throwaway data folder so nothing lands in your real one:

```bash
pnpm --dir src/frontend run build
POLARIS_USER_DATA_DIR=/tmp/polaris-dev \
POLARIS_DESKTOP_ENGINE='command:["/ABS/src/backend/.venv/bin/python","-m","uvicorn","app.main:app","--app-dir","/ABS/src/backend","--host","127.0.0.1","--port","18080"]' \
POLARIS_DATABASE_URL='sqlite+aiosqlite:////tmp/polaris-dev/dev.db' \
POLARIS_DATA_DIR=/tmp/polaris-dev/data \
pnpm --dir src/desktop run dev
```

To use another port, change it in the command and set `POLARIS_DESKTOP_ENGINE_PORT` to the same
number. An engine run this way reports its version as `dev`.

**The first-launch setup, without packaging** (`bootstrap:`). After `pnpm --dir src/desktop run
stage:resources`, `POLARIS_DESKTOP_ENGINE=bootstrap:/ABS/src/desktop/resources` makes the shell
run the same setup as the packaged app against those resources (downloads included), into
`POLARIS_USER_DATA_DIR`.

**The dev/test engine image** (`docker:`; `make engine-image` builds `polaris-api-test:local`, see
[Development](development.md#the-engine-image-for-tests)). The E2E engine group and the docker
smoke group use it (`POLARIS_E2E_ENGINE=1`, `POLARIS_SMOKE_ENGINE=1`).

`pnpm run smoke` needs none of these: besides loading the SPA, it starts a stand-in engine in
command mode and checks the wiring around it (not in the config tree, log file with secrets
removed, diagnostics, automatic restart after a kill, giving up after the second, quitting while
the engine is still starting). `node dist/bootstrap-smoke.cjs --resources <dir>` drives the
first-launch setup with plain Node; `--block-default` points the default download sources at a
dead address to force the mirror path, `--faults` adds the disk-space, failed-upgrade and
cancel-then-resume scenarios, and `--port` moves the engine off 18080.

CI covers both: `desktop-build.yml` runs the smoke test on pull requests that touch the
frontend or the shell, and `desktop-release.yml` builds all three platforms on a `v*` tag
and publishes a GitHub release.

### Interface-only updates and the engine

A release ships the renderer separately as
`renderer-<version>-c<contract>-b<backend hash>.tar.gz` (built once, on Linux — the bundle is
platform-independent). A client applies it in place — swap the bundle, reload, no reinstall —
only when both of these hold (`src/desktop/src/main/updates/engine-compat.ts`):

- `c<contract>` is not higher than the shell's `CONTRACT_VERSION`: the preload bridge is new
  enough;
- `b<backend hash>` equals the hash of the engine that is installed
  (`resources/backend/.hash`, the same content hash the first-launch setup uses): the Python
  engine is the one this interface was built against.

Otherwise the client is offered the installer, and `UpdateInfo.fullReason` says why (`engine`,
`shell` or `no-renderer`). The second check is what was missing before: with the contract number
alone, an install took every new interface, kept its old engine, reported the new version in
About and was never offered the installer again. Shells from before this scheme only recognise
`renderer-<version>-c<contract>.tar.gz`, so they do not see the new bundle at all and fall back to
the installer — which is what an install in that state needs.

The hash is computed by `scripts/stage-backend.mjs` over the staged backend sources, with paths
sorted in `/` form and text files hashed with LF line endings so that all three platforms get the
same value from the same commit. The release job compares the three; if they differ it publishes
without the renderer bundle (installer-only), with a warning.

The same script stamps the engine's version: it writes `app/_release.py` with the version taken
from the release tag, after hashing and outside the hash, so a release that changes only the
version number neither forces every user to rebuild their environment nor blocks an
interface-only update. `/api/health` reports that version; **Settings → About** and the
diagnostics show it next to the app's.

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
  first launch still needs the Gatekeeper step described under [Installing](#installing):
  System Settings → Privacy & Security → Open Anyway, or
  `xattr -dr com.apple.quarantine /Applications/Polaris.app`. Right-click → Open stopped
  working for non-notarized apps in macOS 15.
- **Windows**: prefer the portable zip, which bypasses SmartScreen's installer check.
- **Linux**: AppImage and deb. The deb maintainer comes from `author` in
  `src/desktop/package.json` and **must include an email**, or electron-builder aborts —
  a macOS-only build never exercises that path, so CI is where it surfaces.
  AppImage needs `libnss3 libgtk-3-0 libasound2` on the host. Under Ubuntu
  24.04+ AppArmor restrictions, or without a SUID `chrome-sandbox`, it needs
  `--no-sandbox` — **document that, do not disable the sandbox in code**.

## Backend side

The engine's CORS whitelist always includes `app://polaris` — it is a constant (`DESKTOP_ORIGIN` in
`src/backend/app/core/config.py`), not something to configure. The whitelist matters because every
desktop request carries an `Authorization` header, so every request triggers a preflight, and with
an empty `allow_origins` Starlette answers those preflights with 400. This cannot be worked around
on the client — injecting response headers cannot change a status code.

## Securing the local engine

The engine listens on `127.0.0.1:18080`, but "only this machine can reach it" is not the same as
"only this user's Polaris window can use it": any web page open in a browser can send requests to
a local port, a malicious domain can be re-pointed at `127.0.0.1` (DNS rebinding), and other
accounts on the same computer can connect too. A session on the engine is enough to register an MCP
server with an arbitrary command, so the engine guards its door (#850):

- **No wildcard CORS.** Cross-origin access is allowed only for `app://polaris` and the origins in
  `POLARIS_CORS_ORIGINS`. Other pages cannot read any response.
- **Host header check.** Requests whose `Host` is not `127.0.0.1`, `localhost`, or `[::1]` (any
  port) get `400` — this is what stops DNS rebinding. `POLARIS_ALLOWED_HOSTS` adds names.
- **A per-launch session secret.** Every time the `legacy-engine` plugin starts the engine it
  generates a random secret and passes it as `POLARIS_LOCAL_SESSION_SECRET`. The shell hands the
  same value to its own renderer through `kernel.localBackend` (`sessionSecret`), and
  `lib/local-session.ts` sends it in the `X-Polaris-Session-Secret` header. With the variable set,
  `POST /api/auth/local-session` answers `403` to anyone without it. When the variable is not set
  (running from source, tests), the endpoint instead accepts requests with no `Origin` header or
  an allowed one, so a browser page on another site still gets `403`.
- **Per-install keys.** On first launch the plugin writes `userData/engine-secrets.json` (mode
  `0600`) with a random `POLARIS_SECRET_KEY` (session tokens, signed download links, browser
  extension key digests) and a Fernet `POLARIS_ENCRYPTION_KEY` (stored literature and service keys, SSH
  credentials, MCP server environment variables; the Claude API key is not stored). Earlier versions used the public default secret key and
  derived the encryption key from it. The engine still decrypts values written that way, and at
  startup it re-encrypts every one of them with the new key (`rotate_encrypted_secrets` in
  `app/core/security.py`): idempotent, committed table by table, and safe to interrupt, because
  decryption accepts both keys. The secret-key change signs everyone out once; the renderer
  simply fetches a new session. Browser-extension keys keep working: a key whose digest still
  matches the old default is accepted once and re-hashed.
- **Knowing its own engine.** The plugin also passes a random `POLARIS_INSTANCE_ID`, which
  `/api/health` echoes back; the health check only accepts an answer carrying that id. Before
  starting, if something already answers on the port, the plugin compares it with
  `userData/engine.pid.json`: if it is the engine the shell itself started last time (same
  instance id), the shell stops it and carries on; otherwise it refuses to start and the
  "engine not running" page says whether the port is held by an older Polaris engine or by
  another program. It never stops a process it cannot identify as its own.

The keys travel only through environment variables (docker mode passes them by name, so they
never appear in a command line). They are never in the plugin config tree — the engine is not in
that tree at all — and they are removed from every log line before it is written.
