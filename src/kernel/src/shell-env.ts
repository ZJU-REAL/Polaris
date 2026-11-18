/* ============================================================
   Login-shell environment pickup for the engine's Claude API settings.

   Apps launched from Finder / the Dock / a Linux desktop launcher do not
   inherit what the user exports in ~/.zshrc, ~/.bashrc and friends, so an
   `export ANTHROPIC_API_KEY=…` there never reaches the engine child. Once
   per app launch, before the engine is spawned, we ask the user's login
   shell for its environment and copy over ONLY the names in ENGINE_LLM_ENV
   (and the network / local-agent lists below) that the process does not
   already have. PATH is the one exception: it is always set, so the shell's
   entries are appended to it — local agents (claude-agent-acp, codex-acp,
   pi-acp…) live in Homebrew / npm / nvm directories a Dock-launched app
   cannot see otherwise.

   Rules this module keeps:
   - never throws: timeout / non-zero exit / missing shell → nothing picked up;
   - never logs or returns values to a logger — callers get the NAMES only;
   - an already-set (non-empty) variable always wins over the shell's;
   - Windows is a no-op (there is no rc-file gap: GUI apps see the user env);
   - dependency-free and electron-free (tests/electron-free.test.ts).
   ============================================================ */

import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'

/**
 * The environment variables the engine reads for the Claude API, in the
 * engine's own resolution order (key / endpoint), plus the operator overrides.
 * Command mode inherits the whole host environment, so these pass through on
 * their own; docker mode forwards them by name (see legacy-engine.ts).
 * POLARIS_LLM_FAKE_FALLBACK is deliberately NOT here (#717).
 */
export const ENGINE_LLM_ENV = [
  'POLARIS_CLAUDE_API_KEY',
  'POLARIS_CLAUDE_BASE_URL',
  'POLARIS_CLAUDE_MODEL',
  'POLARIS_EMBEDDING_MODEL',
  'POLARIS_RERANK_MODEL',
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_BASE_URL',
  'LITELLM_API_KEY',
  'LITELLM_API_BASE',
] as const

/**
 * Network settings people export in their shell rc files and expect a GUI app
 * to respect: proxies (read by uv during first-run setup and by the engine),
 * uv's own index/mirror overrides, and the engine's outbound proxy.
 * ensureShellEnv picks these up together with ENGINE_LLM_ENV.
 */
export const SHELL_NETWORK_ENV = [
  'HTTPS_PROXY',
  'HTTP_PROXY',
  'ALL_PROXY',
  'NO_PROXY',
  'https_proxy',
  'http_proxy',
  'all_proxy',
  'no_proxy',
  'UV_DEFAULT_INDEX',
  'UV_INDEX_URL',
  'UV_PYTHON_INSTALL_MIRROR',
  'POLARIS_OUTBOUND_PROXY',
] as const

/**
 * Credentials and config dirs that local coding agents (Claude Code, Codex,
 * pi, Gemini CLI…, connected over ACP) read from the environment. The engine
 * passes each agent only the names its template lists (see the backend's
 * services/acp/templates.py); picking them up here is what lets an agent
 * started from the Dock log in the same way it does in the user's terminal.
 * ANTHROPIC_* are deliberately not agent names: they are Polaris's own key,
 * and Claude Code should keep using the user's own login.
 */
export const AGENT_SHELL_ENV = [
  'CLAUDE_CONFIG_DIR',
  'CODEX_HOME',
  'CODEX_API_KEY',
  'CODEX_PATH',
  'OPENAI_API_KEY',
  'OPENAI_BASE_URL',
  'OPENAI_MODEL',
  'GEMINI_API_KEY',
  'GOOGLE_API_KEY',
  'GOOGLE_CLOUD_PROJECT',
  'GOOGLE_CLOUD_LOCATION',
  'GOOGLE_GENAI_USE_VERTEXAI',
  'GOOGLE_APPLICATION_CREDENTIALS',
  'DASHSCOPE_API_KEY',
  'OPENROUTER_API_KEY',
  'GROQ_API_KEY',
  'XAI_API_KEY',
  'MISTRAL_API_KEY',
  'OPENCODE_CONFIG',
  'KIMI_API_KEY',
  'MOONSHOT_API_KEY',
  'NVM_DIR',
] as const

/**
 * Path-list variables that are always present but much shorter in a GUI app
 * (Finder gives /usr/bin:/bin:/usr/sbin:/sbin). Rather than "fill if missing",
 * the login shell's entries are appended to the ones the process already has,
 * so CLIs installed via Homebrew, npm, nvm, Volta… are found by local agents.
 * Existing entries keep their order and win.
 */
export const SHELL_MERGED_PATHS = ['PATH'] as const

/** Append the entries of `extra` that `current` lacks; separator ':' (POSIX only). */
export function mergePathList(current: string | undefined, extra: string | undefined): string {
  const have = (current ?? '').split(':').filter(Boolean)
  const seen = new Set(have)
  for (const entry of (extra ?? '').split(':')) {
    if (entry && !seen.has(entry)) {
      seen.add(entry)
      have.push(entry)
    }
  }
  return have.join(':')
}

/** Upper bound on how long a slow shell (heavy rc files) may hold up engine start. */
export const SHELL_ENV_TIMEOUT_MS = 4_000

/** Stop reading a runaway shell's stdout past this many bytes. */
const MAX_OUTPUT_BYTES = 4 * 1024 * 1024

export interface ShellEnvMarkers {
  begin: string
  end: string
}

/**
 * Pure parser: pull the wanted names out of a shell's stdout.
 *
 * Only the text between the two markers is looked at, so banners, motd and
 * anything else rc files print are ignored. The dump is either NUL-separated
 * (`env -0`) or newline-separated (`env`); in the newline form a multi-line
 * value of some other variable just yields lines that are not `WANTED=…` and
 * are skipped. The value is everything after the FIRST `=`. Empty values are
 * dropped (an empty key is the same as no key).
 */
export function parseShellEnv(
  stdout: string,
  markers: ShellEnvMarkers,
  names: readonly string[] = ENGINE_LLM_ENV,
): Record<string, string> {
  const found: Record<string, string> = {}
  const begin = stdout.indexOf(markers.begin)
  if (begin < 0) return found
  const start = begin + markers.begin.length
  const end = stdout.indexOf(markers.end, start)
  if (end < 0) return found
  const dump = stdout.slice(start, end)
  const wanted = new Set(names)
  const records = dump.includes('\0') ? dump.split('\0') : dump.split(/\r?\n/)
  for (const record of records) {
    const eq = record.indexOf('=')
    if (eq <= 0) continue
    const name = record.slice(0, eq)
    if (!wanted.has(name)) continue
    const value = record.slice(eq + 1)
    // A wanted name never legitimately holds a multi-line value (keys, URLs,
    // model ids); in the NUL form keep only its first line rather than guess.
    const firstLine = value.split(/\r?\n/, 1)[0] ?? ''
    if (firstLine) found[name] = firstLine
  }
  return found
}

export interface ShellRunOptions {
  timeoutMs: number
  env: NodeJS.ProcessEnv
}

/** Runs `shell args…` and resolves its stdout, or null on any failure. Must never reject. */
export type ShellRunner = (shell: string, args: string[], options: ShellRunOptions) => Promise<string | null>

/** Default runner: a real child process, stdin ignored, killed on timeout. */
export const runShell: ShellRunner = (shell, args, { timeoutMs, env }) =>
  new Promise((resolve) => {
    let settled = false
    let timer: NodeJS.Timeout | undefined
    const finish = (out: string | null): void => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      resolve(out)
    }
    let child: ReturnType<typeof spawn>
    try {
      child = spawn(shell, args, {
        stdio: ['ignore', 'pipe', 'ignore'],
        env,
        // Own session/process group: an interactive shell with no controlling
        // terminal cannot be stopped by SIGTTIN/SIGTTOU, and on timeout the
        // whole group (rc files may have started helpers) is killed at once.
        detached: true,
        windowsHide: true,
      })
    } catch {
      finish(null)
      return
    }
    const kill = (): void => {
      try {
        if (child.pid !== undefined) process.kill(-child.pid, 'SIGKILL')
      } catch {
        try {
          child.kill('SIGKILL')
        } catch {
          /* already gone */
        }
      }
    }
    const chunks: Buffer[] = []
    let size = 0
    child.stdout?.on('data', (chunk: Buffer) => {
      size += chunk.length
      if (size > MAX_OUTPUT_BYTES) {
        kill()
        finish(null)
        return
      }
      chunks.push(chunk)
    })
    child.stdout?.on('error', () => undefined)
    child.once('error', () => finish(null))
    // close, not exit: stdout is fully drained by then
    child.once('close', (code) => finish(code === 0 ? Buffer.concat(chunks).toString('utf8') : null))
    timer = setTimeout(() => {
      kill()
      finish(null)
    }, timeoutMs)
    // No unref() on the child or the timer: under bare Node an otherwise idle
    // event loop would exit with this promise still pending. The timeout
    // already bounds how long the probe can hold anything up.
  })

export interface ShellEnvOptions {
  /** Environment to read from and write into. Default process.env. */
  env?: NodeJS.ProcessEnv
  /** Default process.platform. */
  platform?: NodeJS.Platform
  /** Names to pick up. Default ENGINE_LLM_ENV. */
  names?: readonly string[]
  /** Path-list names to merge (see SHELL_MERGED_PATHS). Default none. */
  merge?: readonly string[]
  /** Default SHELL_ENV_TIMEOUT_MS. */
  timeoutMs?: number
  /** Injectable for tests; default spawns the real shell. */
  run?: ShellRunner
  /** Injectable for tests; default is random per call. */
  markers?: ShellEnvMarkers
}

function defaultShell(env: NodeJS.ProcessEnv, platform: NodeJS.Platform): string {
  const shell = env.SHELL?.trim()
  if (shell) return shell
  return platform === 'darwin' ? '/bin/zsh' : '/bin/bash'
}

/**
 * Ask the login shell for the wanted names that `env` is missing.
 * Returns only what should be added; does not modify `env`. Never throws.
 */
export async function resolveShellEnv(options: ShellEnvOptions = {}): Promise<Record<string, string>> {
  try {
    const env = options.env ?? process.env
    const platform = options.platform ?? process.platform
    if (platform === 'win32') return {}
    const names = options.names ?? ENGINE_LLM_ENV
    const merge = options.merge ?? []
    const missing = names.filter((name) => !env[name])
    const wanted = [...new Set([...missing, ...merge])]
    if (!wanted.length) return {}

    const id = randomBytes(12).toString('hex')
    const markers = options.markers ?? { begin: `__POLARIS_ENV_BEGIN_${id}__`, end: `__POLARIS_ENV_END_${id}__` }
    // `env -0` (macOS, GNU, recent busybox) is unambiguous for values with
    // newlines; plain `env` is the fallback. Kept to syntax that bash, zsh
    // and fish all accept. Markers are plain [A-Za-z0-9_], safe to single-quote.
    const script = `printf '%s' '${markers.begin}'; env -0 || env; printf '%s' '${markers.end}'`
    const run = options.run ?? runShell
    const stdout = await run(defaultShell(env, platform), ['-ilc', script], {
      timeoutMs: options.timeoutMs ?? SHELL_ENV_TIMEOUT_MS,
      env: {
        ...env,
        // Keep common rc-file side effects from stalling a non-terminal shell
        DISABLE_AUTO_UPDATE: 'true',
        ZSH_TMUX_AUTOSTART: 'false',
      },
    })
    if (typeof stdout !== 'string') return {}
    return parseShellEnv(stdout, markers, wanted)
  } catch {
    return {}
  }
}

/**
 * Resolve and set the missing names on `env` (default process.env).
 * Returns the NAMES that were picked up — never the values. Never throws.
 */
export async function applyShellEnv(options: ShellEnvOptions = {}): Promise<string[]> {
  try {
    const env = options.env ?? process.env
    const found = await resolveShellEnv({ ...options, env })
    const merge = new Set(options.merge ?? [])
    const applied: string[] = []
    for (const [name, value] of Object.entries(found)) {
      if (merge.has(name)) {
        const merged = mergePathList(env[name], value)
        if (merged !== (env[name] ?? '')) {
          env[name] = merged
          applied.push(name)
        }
        continue
      }
      if (env[name]) continue // set meanwhile: the process's own value still wins
      env[name] = value
      applied.push(name)
    }
    return applied
  } catch {
    return []
  }
}

let pending: Promise<string[]> | null = null

/**
 * Once per process: the first call starts the lookup against process.env,
 * every later call (kernel restarts within one app launch included) shares
 * the same promise and spawns nothing.
 */
export function ensureShellEnv(): Promise<string[]> {
  pending ??= applyShellEnv({
    names: [...ENGINE_LLM_ENV, ...SHELL_NETWORK_ENV, ...AGENT_SHELL_ENV],
    merge: SHELL_MERGED_PATHS,
  })
  return pending
}
