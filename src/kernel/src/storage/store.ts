/* ============================================================
   SQLite 落地的两个存储：配置树 + 插件元数据 KV。

   SqliteConfigTreeStore 的语义对齐 MemoryConfigTreeStore：save 全量
   覆盖、load 返回与写入逐字段一致的深拷贝。选全量覆盖而不是 diff
   写入，是因为配置树很小（几十个条目），UI 每次编辑都拿着整树，
   diff 只会换来一堆边界 bug；真要增量是 reconciler 的事，不在存储层。

   PluginMetaStore 是各插件的私有 JSON KV：schema 归 _migrations 管，
   数据归各插件自己管，互不越界。
   ============================================================ */

import type { DatabaseSync } from 'node:sqlite'
import type { ConfigEntry, ConfigTreeStore } from '../config/tree.ts'

interface ConfigEntryRow {
  id: string
  parent_id: string | null
  position: number
  name: string
  config: string | null
  disabled: number | null
  has_children: number
}

export class SqliteConfigTreeStore implements ConfigTreeStore {
  readonly #db: DatabaseSync

  constructor(db: DatabaseSync) {
    this.#db = db
  }

  /** 读全树：一次 SELECT 拉平表，再按 parent_id + position 拼回树形。 */
  async load(): Promise<ConfigEntry[]> {
    const rows = this.#db
      .prepare('SELECT * FROM config_entries ORDER BY position')
      .all() as unknown as ConfigEntryRow[]

    const byParent = new Map<string | null, ConfigEntryRow[]>()
    for (const row of rows) {
      const siblings = byParent.get(row.parent_id) ?? []
      siblings.push(row)
      byParent.set(row.parent_id, siblings)
    }

    const build = (parentId: string | null): ConfigEntry[] =>
      (byParent.get(parentId) ?? []).map((row) => {
        const entry: ConfigEntry = { id: row.id, name: row.name }
        // 只还原写入时存在的键：undefined 键落库时记 NULL，读回时不补键，
        // 保证 load(save(x)) 与内存实现的 structuredClone 语义一致
        if (row.config !== null) entry.config = JSON.parse(row.config)
        if (row.disabled !== null) entry.disabled = row.disabled !== 0
        if (row.has_children) entry.children = build(row.id)
        return entry
      })

    return build(null)
  }

  /** 写全树：事务内清空重写。整树替换让 save 天然幂等且原子。 */
  async save(entries: ConfigEntry[]): Promise<void> {
    const insert = this.#db.prepare(
      `INSERT INTO config_entries (id, parent_id, position, name, config, disabled, has_children)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
    )
    const walk = (list: ConfigEntry[], parentId: string | null): void => {
      list.forEach((entry, position) => {
        insert.run(
          entry.id,
          parentId,
          position,
          entry.name,
          entry.config === undefined ? null : JSON.stringify(entry.config),
          entry.disabled === undefined ? null : entry.disabled ? 1 : 0,
          entry.children === undefined ? 0 : 1,
        )
        if (entry.children) walk(entry.children, entry.id)
      })
    }

    this.#db.exec('BEGIN')
    try {
      this.#db.exec('DELETE FROM config_entries')
      walk(entries, null)
      this.#db.exec('COMMIT')
    } catch (error) {
      this.#db.exec('ROLLBACK')
      throw error
    }
  }
}

/** 插件元数据 KV：值统一 JSON 编解码，键不存在返回 undefined。 */
export class PluginMetaStore {
  readonly #db: DatabaseSync

  constructor(db: DatabaseSync) {
    this.#db = db
  }

  get(key: string): unknown {
    const row = this.#db
      .prepare('SELECT value FROM plugin_meta WHERE key = ?')
      .get(key) as { value: string } | undefined
    return row === undefined ? undefined : JSON.parse(row.value)
  }

  set(key: string, value: unknown): void {
    // upsert 而不是 delete+insert：单语句原子，也保住主键上的行锁语义
    this.#db
      .prepare(
        `INSERT INTO plugin_meta (key, value, updated_at) VALUES (?, ?, ?)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
      )
      .run(key, JSON.stringify(value), new Date().toISOString())
  }

  /** 删除键，返回是否真的删了（不存在时 false）。 */
  delete(key: string): boolean {
    const result = this.#db.prepare('DELETE FROM plugin_meta WHERE key = ?').run(key)
    return result.changes > 0
  }

  /** 全部键，按字典序——稳定次序方便调试与测试断言。 */
  list(): string[] {
    const rows = this.#db
      .prepare('SELECT key FROM plugin_meta ORDER BY key')
      .all() as unknown as { key: string }[]
    return rows.map((row) => row.key)
  }
}
