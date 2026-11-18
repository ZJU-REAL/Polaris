/* ============================================================
   Size-capped log files with a few rotated generations, plus the
   secret scrubber every line passes through before it reaches disk.

   Why this lives in the kernel: the engine child's stdout/stderr is
   captured inside the legacy-engine plugin, and the desktop main process
   wants the very same file format for its own log. One implementation,
   electron-free (tests/electron-free.test.ts), synchronous appends so a
   crash right after a line still leaves that line on disk.

   Rotation: when the live file would grow past `maxBytes`, it is renamed
   to `<name>.1` (older generations shift up, the oldest is dropped), and a
   fresh file is started. Worst case on disk is `(keep + 1) * maxBytes`.

   Secrets: values of environment variables whose NAME ends in KEY, TOKEN,
   SECRET or PASSWORD are replaced by `[redacted:NAME]`, wherever they show
   up in a line; credentials embedded in URLs and a few well-known token
   shapes are masked too. A log a user is asked to paste into a public
   issue must never carry their API key.
   ============================================================ */

import { appendFileSync, closeSync, mkdirSync, openSync, readSync, renameSync, rmSync, statSync } from 'node:fs'
import { dirname } from 'node:path'

/** Names whose values must never be written to a log. */
const SECRET_NAME_RE = /(KEY|TOKEN|SECRET|PASSWORD|PASSWD)S?$/i

/** Shorter values are too likely to be ordinary words/numbers to blank out safely. */
const MIN_SECRET_LENGTH = 8

const PATTERNS: [RegExp, string][] = [
  // user:password@ in any URL (proxy settings are the usual carrier)
  [/\b([a-z][a-z0-9+.-]*:\/\/)[^\s/:@]+:[^\s/@]+@/gi, '$1[redacted]@'],
  // Anthropic / OpenAI style keys
  [/\bsk-[A-Za-z0-9_-]{16,}/g, '[redacted:api-key]'],
  // Authorization headers
  [/\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}/g, '$1 [redacted]'],
  // key=value / "key": "value" where the key itself says it is a secret
  [
    /((?:api[_-]?key|access[_-]?token|token|secret|password|passwd|session[_-]?secret)["']?\s*[=:]\s*["']?)[^\s"'&,;]{6,}/gi,
    '$1[redacted]',
  ],
]

export type Scrubber = (text: string) => string

/**
 * Build a scrubber over one or more environments. The environments are
 * read at call time (cached briefly), so variables picked up after the
 * scrubber was created — the login-shell lookup, the per-launch session
 * secret — are covered as well.
 */
export function createSecretScrubber(environments: () => (Record<string, string | undefined> | undefined)[]): Scrubber {
  let cachedAt = 0
  let cached: [string, string][] = []
  const secrets = (): [string, string][] => {
    const now = Date.now()
    if (now - cachedAt < 2_000) return cached
    const found = new Map<string, string>()
    for (const env of environments()) {
      if (!env) continue
      for (const [name, value] of Object.entries(env)) {
        if (!value || value.length < MIN_SECRET_LENGTH) continue
        if (!SECRET_NAME_RE.test(name)) continue
        if (!found.has(value)) found.set(value, name)
      }
    }
    // longest first: a value that contains another must be replaced whole
    cached = [...found.entries()].sort((a, b) => b[0].length - a[0].length)
    cachedAt = now
    return cached
  }
  return (text) => {
    let out = text
    for (const [value, name] of secrets()) {
      if (out.includes(value)) out = out.split(value).join(`[redacted:${name}]`)
    }
    for (const [re, replacement] of PATTERNS) out = out.replace(re, replacement)
    return out
  }
}

/** Scrubber over the current process environment only. */
export const scrubProcessSecrets: Scrubber = createSecretScrubber(() => [process.env])

export interface RotatingLogOptions {
  /** Absolute path of the live file, e.g. <userData>/logs/engine.log. */
  path: string
  /** Size cap of one generation. Default 2 MiB. */
  maxBytes?: number
  /** Rotated generations kept beside the live file (`.1` … `.N`). Default 3. */
  keep?: number
  /** Applied to every chunk before it is written. Default: scrubProcessSecrets. */
  scrub?: Scrubber
  /** Prefix each line with an ISO timestamp. Default true. */
  timestamps?: boolean
}

export class RotatingLog {
  readonly path: string
  readonly #maxBytes: number
  readonly #keep: number
  readonly #scrub: Scrubber
  readonly #timestamps: boolean
  #size = 0
  #broken = false

  constructor(options: RotatingLogOptions) {
    this.path = options.path
    this.#maxBytes = Math.max(1024, options.maxBytes ?? 2 * 1024 * 1024)
    this.#keep = Math.max(0, options.keep ?? 3)
    this.#scrub = options.scrub ?? scrubProcessSecrets
    this.#timestamps = options.timestamps ?? true
    try {
      mkdirSync(dirname(this.path), { recursive: true })
      this.#size = statSync(this.path).size
    } catch {
      this.#size = 0
    }
  }

  /** Append one or more lines. Never throws: a log that cannot be written must not take the app down. */
  write(text: string): void {
    if (this.#broken) return
    const stamp = this.#timestamps ? `${new Date().toISOString()} ` : ''
    let out = ''
    for (const line of this.#scrub(text).split(/\r?\n/)) {
      if (line.trim()) out += `${stamp}${line}\n`
    }
    if (!out) return
    const bytes = Buffer.byteLength(out)
    try {
      if (this.#size > 0 && this.#size + bytes > this.#maxBytes) this.#rotate()
      appendFileSync(this.path, out)
      this.#size += bytes
    } catch {
      // Disk full / directory removed underneath us: retry once from scratch,
      // then give up for this session rather than throw on every line.
      try {
        mkdirSync(dirname(this.path), { recursive: true })
        appendFileSync(this.path, out)
        this.#size = statSync(this.path).size
      } catch {
        this.#broken = true
      }
    }
  }

  #rotate(): void {
    if (this.#keep === 0) {
      rmSync(this.path, { force: true })
    } else {
      rmSync(`${this.path}.${this.#keep}`, { force: true })
      for (let i = this.#keep - 1; i >= 1; i--) {
        try {
          renameSync(`${this.path}.${i}`, `${this.path}.${i + 1}`)
        } catch {
          /* that generation does not exist yet */
        }
      }
      renameSync(this.path, `${this.path}.1`)
    }
    this.#size = 0
  }

  /** The last `lines` lines, reaching into the previous generation when the live file is short. */
  tail(lines: number): string[] {
    return tailLogFile(this.path, lines)
  }
}

function readTail(path: string, maxBytes: number): string {
  let fd: number | undefined
  try {
    const { size } = statSync(path)
    const length = Math.min(size, maxBytes)
    if (length <= 0) return ''
    fd = openSync(path, 'r')
    const buffer = Buffer.alloc(length)
    readSync(fd, buffer, 0, length, size - length)
    const text = buffer.toString('utf8')
    // a read that starts mid-file starts mid-line: drop the partial first line
    return length < size ? text.slice(text.indexOf('\n') + 1) : text
  } catch {
    return ''
  } finally {
    if (fd !== undefined) closeSync(fd)
  }
}

/** Last `lines` lines of a rotating log file (live file, then `.1` if needed). Never throws. */
export function tailLogFile(path: string, lines: number, maxBytes = 512 * 1024): string[] {
  const split = (text: string): string[] => text.split('\n').filter((line) => line.length > 0)
  let out = split(readTail(path, maxBytes))
  if (out.length < lines) out = [...split(readTail(`${path}.1`, maxBytes)), ...out]
  return out.slice(-lines)
}
