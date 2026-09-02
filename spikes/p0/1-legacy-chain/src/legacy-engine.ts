/* P0 Spike 1: the legacy-engine plugin prototype.

   Mounts the existing Python backend (api + arq worker) plus Redis as
   supervised docker child processes under a cordis context. Every spawn is a
   fiber effect, so ctx.dispose() reclaims all three containers (attached
   `docker run --sig-proxy` children die with the node process tree — the
   orphan-check in the spike test verifies exactly that).

   SQLite is the database (backend default), on a shared named volume so api
   and worker see the same file. The LLM layer runs on the deterministic fake
   provider (POLARIS_LLM_FAKE_FALLBACK=1), so the whole chain is key-free and
   reproducible. */

import { spawn, execFileSync, type ChildProcess } from 'node:child_process'
import { setTimeout as delay } from 'node:timers/promises'
import type { Context } from '@polaris/kernel'

export const PREFIX = 'spike1'
export const API_PORT = 18010
export const IMAGE = 'polaris-api-test:local'

const NET = `${PREFIX}-net`
const DBVOL = `${PREFIX}-db`

function docker(...args: string[]): string {
  return execFileSync('docker', args, { encoding: 'utf8' }).trim()
}

/** Remove leftovers from previous runs (idempotent). */
export function cleanupDocker(): void {
  for (const name of [`${PREFIX}-redis`, `${PREFIX}-api`, `${PREFIX}-worker`]) {
    try {
      docker('rm', '-f', name)
    } catch {
      /* not running */
    }
  }
  try {
    docker('volume', 'rm', '-f', DBVOL)
  } catch {
    /* absent */
  }
  try {
    docker('network', 'rm', NET)
  } catch {
    /* absent */
  }
}

export function dockerAvailable(): boolean {
  try {
    docker('image', 'inspect', IMAGE, '--format', '{{.Id}}')
    return true
  } catch {
    return false
  }
}

export function runningContainers(): string[] {
  return docker('ps', '--format', '{{.Names}}')
    .split('\n')
    .filter((n) => n.startsWith(`${PREFIX}-`))
}

interface EngineOptions {
  backendDir: string
}

const COMMON_ENV = [
  '-e', 'POLARIS_LLM_FAKE_FALLBACK=1',
  '-e', 'POLARIS_REDIS_URL=redis://spike1-redis:6379/0',
  '-e', 'POLARIS_DATABASE_URL=sqlite+aiosqlite:////dbvol/polaris.db',
]

export interface LegacyEngine {
  baseUrl: string
  children: ChildProcess[]
  /** Captured worker stdout+stderr (arq startup banner lands here). */
  workerLog: () => string
}

/** Install the legacy engine into a context; resolves when /api/health is up. */
export async function applyLegacyEngine(ctx: Context, options: EngineOptions): Promise<LegacyEngine> {
  docker('network', 'create', NET)
  docker('volume', 'create', DBVOL)
  const children: ChildProcess[] = []
  const logs = new Map<string, string[]>()

  const spawnContainer = (name: string, args: string[]): ChildProcess => {
    const child = spawn('docker', ['run', '--rm', '--name', name, '--network', NET, ...args], {
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const buf: string[] = []
    logs.set(name, buf)
    child.stderr!.on('data', (d: Buffer) => buf.push(d.toString()))
    child.stdout!.on('data', (d: Buffer) => buf.push(d.toString()))
    children.push(child)
    return child
  }

  ctx.effect(() => {
    spawnContainer(`${PREFIX}-redis`, ['redis:7-alpine'])
    spawnContainer(`${PREFIX}-api`, [
      '-v', `${options.backendDir}:/srv/backend`,
      '-v', `${DBVOL}:/dbvol`,
      '-w', '/srv/backend',
      '-p', `127.0.0.1:${API_PORT}:8000`,
      ...COMMON_ENV,
      IMAGE,
      'sh', '-lc',
      'python -m alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000',
    ])
    spawnContainer(`${PREFIX}-worker`, [
      '-v', `${options.backendDir}:/srv/backend`,
      '-v', `${DBVOL}:/dbvol`,
      '-w', '/srv/backend',
      ...COMMON_ENV,
      IMAGE,
      'sh', '-lc',
      // The worker must not race the api's alembic migration.
      'sleep 5 && exec arq worker.settings.WorkerSettings',
    ])
    return () => {
      for (const name of [`${PREFIX}-worker`, `${PREFIX}-api`, `${PREFIX}-redis`]) {
        try {
          docker('rm', '-f', name)
        } catch {
          /* already gone */
        }
      }
      try {
        docker('volume', 'rm', '-f', DBVOL)
      } catch {
        /* busy */
      }
      try {
        docker('network', 'rm', NET)
      } catch {
        /* busy */
      }
    }
  }, 'legacy-engine containers')

  const baseUrl = `http://127.0.0.1:${API_PORT}`
  const deadline = Date.now() + 120_000
  for (;;) {
    try {
      const res = await fetch(`${baseUrl}/api/health`)
      if (res.ok) break
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) throw new Error('legacy engine did not become healthy in 120s')
    await delay(1000)
  }
  return { baseUrl, children, workerLog: () => (logs.get(`${PREFIX}-worker`) ?? []).join('') }
}
