/* Supervised Python sidecar: spawn, JSON-RPC endpoint, restart on death.
   Prototype for the kernel's python-edge plugin (P1); findings feed the
   P0 gate report. */

import { spawn, type ChildProcess } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { JsonRpcEndpoint, type NotificationHandler } from '@polaris/kernel'

const here = dirname(fileURLToPath(import.meta.url))
export const SIDECAR = join(here, '..', 'sidecar.py')

export interface SupervisorEvents {
  onExit?: (code: number | null, signal: string | null) => void
  onNotification?: NotificationHandler
}

export class Sidecar {
  child!: ChildProcess
  rpc!: JsonRpcEndpoint
  restarts = 0

  constructor(private readonly events: SupervisorEvents = {}) {}

  start(): void {
    this.child = spawn('python3', [SIDECAR], { stdio: ['pipe', 'pipe', 'inherit'] })
    this.rpc = new JsonRpcEndpoint(this.child.stdin!, this.child.stdout!, {
      onNotification: this.events.onNotification,
    })
    this.child.on('exit', (code, signal) => {
      // Deterministic rejection of every in-flight request; the reconciler
      // decides whether to respawn.
      this.rpc.rejectAll(`sidecar exited (code=${code}, signal=${signal})`)
      this.events.onExit?.(code, signal)
    })
  }

  async stop(): Promise<void> {
    this.child.kill()
    await new Promise((r) => this.child.once('exit', r))
  }
}
