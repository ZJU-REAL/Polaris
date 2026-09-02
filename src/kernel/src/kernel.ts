/* ============================================================
   Polaris kernel: the cordis runtime host.

   The kernel owns exactly one root Context. Everything else — the config
   tree, process supervisors, the legacy engine, storage, the gateway — is a
   plugin installed into this context, so that install/uninstall/upgrade are
   effect-reversible operations (see vendor/deepseek-cordis).

   HARD CONSTRAINT: this package must stay electron-free. It is mounted by
   the desktop main process in P1, but it must always be startable under
   bare Node (spikes, CI regression, future headless mode). Enforced by
   tests/electron-free.test.ts.
   ============================================================ */

import { Context } from '@deepseek-ai/cordis'

export interface KernelOptions {
  /** Human-readable instance name, used in logs and diagnostics. */
  name?: string
}

export class Kernel {
  readonly ctx: Context
  readonly name: string
  #started = false
  #stopped = false

  constructor(options: KernelOptions = {}) {
    this.name = options.name ?? 'polaris'
    this.ctx = new Context()
  }

  get started(): boolean {
    return this.#started && !this.#stopped
  }

  /**
   * Start the kernel. Baseline plugins (config tree, supervisors) are
   * installed by the caller or by profile code; start itself only marks the
   * kernel live. Idempotent.
   */
  async start(): Promise<void> {
    if (this.#stopped) throw new Error('kernel already stopped')
    this.#started = true
  }

  /**
   * Stop the kernel: dispose the root fiber, which cascades disposal through
   * every installed plugin and reclaims all registered effects (timers,
   * listeners, child processes). Idempotent.
   */
  async stop(): Promise<void> {
    if (this.#stopped) return
    this.#stopped = true
    await this.ctx.fiber.dispose()
  }
}

/** Create a kernel instance. See {@link Kernel}. */
export function createKernel(options: KernelOptions = {}): Kernel {
  return new Kernel(options)
}
