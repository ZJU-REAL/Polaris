# Getting started

Polaris is a desktop app. It runs everything on your own computer — a local engine with a SQLite
database — so there is no server to set up, no account to create, and nothing to configure before
the first launch. This guide takes you from installing the app to your first literature library.
For working on Polaris itself, see [Development](development.md); for the environment variables the
engine reads, see [Configuration](configuration.md).

## 1. Install the app

Download the installer for your platform from
[Releases](https://github.com/ZJU-REAL/Polaris/releases/latest):

| Platform | Files |
| --- | --- |
| macOS (universal) | `.dmg` or `.zip` |
| Windows | `.exe` installer or portable `.zip` |
| Linux | `.AppImage` or `.deb` |

The builds are **neither signed nor notarized**, so each platform needs to be told once that the app
is safe to run:

- **macOS**: run `xattr -dr com.apple.quarantine /Applications/Polaris.app`, or right-click the app
  and choose Open.
- **Windows**: at the SmartScreen prompt choose More info → Run anyway (the portable zip skips this
  prompt).
- **Linux**: the AppImage needs `libnss3 libgtk-3-0 libasound2`; under Ubuntu 24.04+ AppArmor
  restrictions start it with `--no-sandbox`.

> [!NOTE]
> Releases up to v0.3.9 are remote-only shells that always ask for a server address, and they cannot
> update themselves into the local build. Install a current release over the old app.

## 2. First launch

On first launch the app sets up its engine: it downloads a Python toolchain and the engine's
dependencies into its own data folder, then starts the engine and opens the main window. A progress
page shows each step. This can take a few minutes; later launches skip it and start right away.

There is no sign-in: you are the only user and the owner of everything in the app. Your data —
the SQLite database, PDFs, exports, experiment logs — lives in the app's user-data folder; see
[where the data lives](desktop.md#where-the-data-lives).

The app checks for updates (Settings → About) and applies them without a reinstall where it can.

## 3. Give Polaris a model

Go to **Settings → Models & agents** (`/settings?tab=llm`). Until a model is available, AI features
return `LLM_NOT_CONFIGURED`.

- **Easiest: an agent backend.** If you already use Claude Code, Codex, or Gemini CLI, install it
  and sign in on the same computer, then add it under **Agent backends** and click
  **Check connection**. With no model API configured, the first enabled agent answers every model
  call — no API key needed. See [Agent backends](agents.md).
- **Optional: a model API.** Add a provider (OpenAI-compatible, OpenAI Responses, or Anthropic) with
  its key and map research stages to it in the routing table. Embeddings and reranking can only come
  from a model API; without one, search falls back to keyword matching.

<!-- screenshot: Settings → Models & agents, the model routing table -->

## 4. Next steps in the app

Work through these in order; each unlocks the next.

1. **Connect an SSH server** (needed for the experiment stage) — **Settings → SSH credentials**
   (`/settings?tab=ssh`). Add a host and key, then use **Test connection**; credentials are
   encrypted at rest. Experiment policy (command allow/deny lists, budgets) lives under
   **Settings → Experiments**.
2. **Create your first direction library** — go to **Libraries** (`/libraries`) and create one. A
   structured AI interview helps you write the inclusion config (statement, goals, scope,
   exclusions), and running the ingest builds the corpus: candidate search, citation snowballing,
   relevance scoring, full-text extraction, and wiki compilation, all as one resumable task.
3. **Create a topic and link libraries** — create a project (`/projects/new`), then link one or
   more direction libraries to it. A topic holds no papers of its own; its corpus is the union of
   the libraries linked to it. From there, work through the pipeline stage by stage — see
   [Core concepts](concepts.md).
4. **Optional: configure the daily feed** — **Settings → Daily papers** sets the subscribed
   categories and the daily fetch time. The engine checks every few minutes whether the fetch is
   due, so the feed only updates while the app is running.
5. **Optional: enable PolarisBuddy's tool loop** — the in-app assistant's multi-turn tool loop is
   off by default (it re-sends history and tool schemas every round, so it costs more than one-shot
   chat). It is switched on by the environment variable `POLARIS_CHAT_AGENT_ENABLED=1`; the engine
   inherits the app's environment, so start the app from a terminal with that variable set.

> [!WARNING]
> The Experiment Lab connects to real GPU servers over SSH and runs generated code there. Every
> remote command comes from a fixed whitelist of templates and is written to the audit log, and an
> experiment can be created with an explicit compute-budget approval — but you should still point
> Polaris only at machines you control and review the audit log.

## Common first-run problems

| Symptom | Cause and fix |
| --- | --- |
| The progress page stops at `python` or `install` | The first launch downloads a Python toolchain and packages. Check the network (or proxy) and restart the app; the setup starts over cleanly. |
| macOS says the app is "damaged" or cannot be opened | Quarantine flag on an unsigned build. Run `xattr -dr com.apple.quarantine /Applications/Polaris.app`. |
| AI features return `LLM_NOT_CONFIGURED` (HTTP 503) | No agent backend is enabled and no model API route is usable. Add an agent or a model API in **Settings → Models & agents**. |
| An agent backend fails **Check connection** | The agent is not installed or not signed in on this computer. The error message names the install or sign-in command to run. |
| Literature ingest finds nothing / arXiv, Semantic Scholar, or OpenAlex time out | Direct access to the literature APIs is blocked or flaky on your network. Set an outbound proxy for literature APIs (see [Configuration](configuration.md)). |
| LaTeX compile says the compiler is not installed | Manuscripts compile with `tectonic` (or TeX Live through `latexmk`) installed on your computer. Install one and make sure it is on your `PATH`. |

## Next steps

- [Introduction](index.md): what each pipeline stage does, with links to the per-stage guides.
- [Core concepts](concepts.md): the pipeline, Voyages, skills, and the MCP tools.
- [Agent backends](agents.md): using Claude Code and other agents as Polaris's model.
- [Desktop app](desktop.md): how the app starts its engine, and where your data lives.
- [Development](development.md): running Polaris from source, migrations, tests, and the Git
  conventions.
