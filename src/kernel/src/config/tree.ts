/* ============================================================
   Config tree — module boundary stub.

   The declarative plugin configuration tree is the single source of truth
   for which components are enabled with which config. Each process (kernel,
   Python edge, remote runner) runs a reconciler that diffs the desired tree
   against its live fibers and applies the minimal effect set.

   Spike 3 delivers the cross-process reconciler prototype against this
   interface; the SQLite-backed store lands with the kernel storage plugin
   (P1-A5). Until then this file only fixes the shape of the boundary.
   ============================================================ */

export interface ConfigEntry {
  /** Stable entry id (hierarchical, `:`-separated). */
  id: string
  /** Plugin name the entry instantiates, or a group marker. */
  name: string
  /** Plugin config payload (validated by the plugin's schema on apply). */
  config?: unknown
  /** Disabled entries stay in the tree but are not instantiated. */
  disabled?: boolean
  /** Child entries (groups). */
  children?: ConfigEntry[]
}

export interface ConfigTreeStore {
  /** Read the full desired tree. */
  load(): Promise<ConfigEntry[]>
  /** Persist the full desired tree (UI edits write through here). */
  save(entries: ConfigEntry[]): Promise<void>
}

/** In-memory store, used by tests and the spikes. */
export class MemoryConfigTreeStore implements ConfigTreeStore {
  #entries: ConfigEntry[] = []

  async load(): Promise<ConfigEntry[]> {
    return structuredClone(this.#entries)
  }

  async save(entries: ConfigEntry[]): Promise<void> {
    this.#entries = structuredClone(entries)
  }
}
