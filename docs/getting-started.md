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

## 3. Give Polaris a Claude API key

Polaris uses the Claude API with one fixed model (`claude-opus-5`). The key comes from an
environment variable; there is nothing to fill in inside the app. Until a key is available, AI
features return `LLM_NOT_CONFIGURED`.

1. **Set the environment variable.** On macOS or Linux, add `export ANTHROPIC_API_KEY=sk-ant-...`
   to `~/.zshrc` or `~/.bashrc`. If you use a LiteLLM gateway instead, export `LITELLM_API_KEY` and
   `LITELLM_API_BASE` (for example `http://your-gateway:4000`). On Windows, set them as user
   environment variables.
2. **Restart Polaris.** The variables are read once at startup.
3. **Check under Settings → Model** (`/settings?tab=model`). The page shows the model and the host
   requests go to; click **Check connection** to make one small live call.

Search by meaning and re-ranking need a gateway address: with a key for `api.anthropic.com` alone,
search falls back to keyword matching and everything else works. The full list of variables is in
[Configuration](configuration.md#model).

<!-- screenshot: Settings → Model, the read-only status page -->

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
5. **Assistant** — PolarisBuddy, the in-app assistant, works as soon as the Claude API key is
   set. Its multi-turn tool loop re-sends history and tool definitions every round, so it uses
   more than one-shot chat; to turn it off, start the app with `POLARIS_CHAT_AGENT_ENABLED=0`.
6. **Optional: connect a local agent** — if you already use Claude Code, Codex or pi, add it under
   **Settings → Local agents** (`/settings?tab=agents`) and click **Check connection**. In the
   assistant, pick it under **Answered by** for a conversation; it runs with its own sign-in on
   this computer. See [Local agents](configuration.md#local-agents).

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
| AI features return `LLM_NOT_CONFIGURED` (HTTP 503) | Polaris found no Claude API key. Set `ANTHROPIC_API_KEY` (or the `LITELLM_API_KEY` / `LITELLM_API_BASE` pair) as an environment variable and reopen Polaris; see [step 3](#3-give-polaris-a-claude-api-key). If you just added it to `~/.zshrc`, quit Polaris fully and open it again. |
| Literature ingest finds nothing / arXiv, Semantic Scholar, or OpenAlex time out | Direct access to the literature APIs is blocked or flaky on your network. Set an outbound proxy for literature APIs (see [Configuration](configuration.md)). |
| LaTeX compile says the compiler is not installed | Manuscripts compile with `tectonic` (or TeX Live through `latexmk`) installed on your computer. Install one and make sure it is on your `PATH`. |

## Next steps

- [Introduction](index.md): what each pipeline stage does, with links to the per-stage guides.
- [Core concepts](concepts.md): the pipeline, Voyages, skills, and the MCP tools.
- [Configuration](configuration.md#model): the Claude API variables and what Settings → Model shows.
- [Desktop app](desktop.md): how the app starts its engine, and where your data lives.
- [Development](development.md): running Polaris from source, migrations, tests, and the Git
  conventions.
