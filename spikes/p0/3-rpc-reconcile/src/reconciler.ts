/* Cross-process config-tree reconciler prototype.

   The kernel holds the desired tree; each edge process owns a live subset.
   reconcile() diffs desired against the sidecar's reported state, pushes one
   config.apply with the full desired set (the sidecar computes and reports
   its own minimal action set), and verifies convergence via config.report.
   Every externally visible step lands in an effect log so tests can assert
   the minimal effect set. Crash recovery: on sidecar death the supervisor
   respawns an empty process and reconcile() re-applies the desired tree —
   state lives kernel-side only. */

import type { ConfigEntry } from '@polaris/kernel'
import { Sidecar } from './supervisor.ts'

export interface Effect {
  op: 'spawn' | 'stop' | 'start' | 'update' | 'apply' | 'converged'
  id?: string
}

export class Reconciler {
  readonly effects: Effect[] = []
  desired: ConfigEntry[] = []
  sidecar!: Sidecar
  #reconciling: Promise<void> | null = null

  async startProcess(): Promise<void> {
    this.sidecar = new Sidecar({
      onNotification: (method, params) => {
        if (method === 'config.progress') {
          const { op, id } = params as { op: Effect['op']; id: string }
          this.effects.push({ op, id })
        }
      },
      onExit: () => {
        // Crash: respawn empty and drive back to the desired tree.
        this.effects.push({ op: 'spawn' })
        this.sidecar.restarts++
        this.sidecar.start()
        void this.reconcile()
      },
    })
    this.effects.push({ op: 'spawn' })
    this.sidecar.start()
    await this.sidecar.rpc.call('initialize')
  }

  /** Set the desired tree and drive the sidecar to it. */
  async setDesired(entries: ConfigEntry[]): Promise<void> {
    this.desired = entries
    await this.reconcile()
  }

  async reconcile(): Promise<void> {
    // Serialize overlapping reconcile calls (crash recovery vs user edits).
    while (this.#reconciling) await this.#reconciling
    this.#reconciling = this.#doReconcile()
    try {
      await this.#reconciling
    } finally {
      this.#reconciling = null
    }
  }

  async #doReconcile(): Promise<void> {
    const enabled = this.desired.filter((e) => !e.disabled)
    await this.sidecar.rpc.call('config.apply', { entries: enabled })
    this.effects.push({ op: 'apply' })
    const report = (await this.sidecar.rpc.call('config.report')) as { entries: ConfigEntry[] }
    const got = JSON.stringify([...report.entries].sort((a, b) => a.id.localeCompare(b.id)))
    const want = JSON.stringify([...enabled].sort((a, b) => a.id.localeCompare(b.id)))
    if (got !== want) throw new Error(`not converged:\n got ${got}\nwant ${want}`)
    this.effects.push({ op: 'converged' })
  }

  /** Component-level effects observed since the given index. */
  componentEffects(since = 0): Effect[] {
    return this.effects.slice(since).filter((e) => ['start', 'stop', 'update'].includes(e.op))
  }
}
